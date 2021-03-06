"""
Simplified Data Loading
"""
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.sampler import Sampler
from torch.utils.data.distributed import DistributedSampler
import torch
from torch.nn import functional as F
from pathlib import Path
from _init_stuff import Fpath, Arr, yaml, DF, ForkedPdb
from yacs.config import CfgNode as CN
import pandas as pd
import h5py

import numpy as np
import json
import copy
from typing import Dict
from munch import Munch
from trn_utils import DataWrap

import ast
import pickle
from contrastive_sampling import create_similar_list, create_random_list
from mdl_srl_utils import combine_first_ax
from trn_utils import get_dataloader

torch.multiprocessing.set_sharing_strategy('file_system')


class AnetEntDataset(Dataset):
    """
    Dataset class adopted from
    https://github.com/facebookresearch/grounded-video-description
    /blob/master/misc/dataloader_anet.py#L27
    This is basically AE loader.
    """

    def __init__(self, cfg: CN, ann_file: Fpath, split_type: str = 'train',
                 comm: Dict = None):
        self.cfg = cfg

        # Common stuff that needs to be passed around
        if comm is not None:
            assert isinstance(comm, (dict, Munch))
            self.comm = Munch(comm)
        else:
            self.comm = Munch()

        self.split_type = split_type
        self.ann_file = Path(ann_file)
        assert self.ann_file.suffix == '.csv'
        self.set_args()
        self.load_annotations()

        # self.create_glove_stuff()

        h5_proposal_file = h5py.File(
            self.proposal_h5, 'r', driver='core')
        self.num_proposals = h5_proposal_file['dets_num'][:]
        self.label_proposals = h5_proposal_file['dets_labels'][:]

        self.itemgetter = getattr(self, 'simple_item_getter')
        self.test_mode = (split_type != 'test')
        self.after_init()

    def after_init(self):
        pass

    def set_args(self):
        """
        Define the arguments to be used from the cfg
        """
        # NOTE: These are changed at extended_config/post_proc_config
        dct = self.cfg.ds[f'{self.cfg.ds.exp_setting}']
        self.proposal_h5 = Path(dct['proposal_h5'])
        self.feature_root = Path(dct['feature_root'])

        # Max proposals to be considered
        # By default it is 10 * 100
        self.num_frms = self.cfg.ds.num_sampled_frm
        self.num_prop_per_frm = dct['num_prop_per_frm']
        self.comm.num_prop_per_frm = self.num_prop_per_frm
        self.max_proposals = self.num_prop_per_frm * self.num_frms

        # Assert h5 file to read from exists
        assert self.proposal_h5.exists()

        # Assert region features exists
        assert self.feature_root.exists()

        # Assert rgb, motion features exists
        self.seg_feature_root = Path(self.cfg.ds.seg_feature_root)
        assert self.seg_feature_root.exists()

        # Which proposals to be included
        self.prop_thresh = self.cfg.misc.prop_thresh
        self.exclude_bgd_det = self.cfg.misc.exclude_bgd_det

        # Assert raw caption file (from activity net captions) exists
        self.raw_caption_file = Path(self.cfg.ds.anet_cap_file)
        assert self.raw_caption_file.exists()

        # Assert act ent caption file with bbox exists
        self.anet_ent_annot_file = Path(self.cfg.ds.anet_ent_annot_file)
        assert self.anet_ent_annot_file.exists()

        # Assert word vocab files exist
        self.dic_anet_file = Path(self.cfg.ds.anet_ent_split_file)
        assert self.dic_anet_file.exists()

        # Max gt box to consider
        # should consider all, set high
        self.max_gt_box = self.cfg.ds.max_gt_box

        # temporal attention size
        self.t_attn_size = self.cfg.ds.t_attn_size

        # Sequence length
        self.seq_length = self.cfg.ds.max_seq_length

    def load_annotations(self):
        """
        Process the annotation file.
        """
        # Load annotation files
        self.annots = pd.read_csv(self.ann_file)

        # Load raw captions
        with open(self.raw_caption_file) as f:
            self.raw_caption = json.load(f)

        # Load anet bbox
        with open(self.anet_ent_annot_file) as f:
            self.anet_ent_captions = json.load(f)

        # Needs to exported as well

        # Load dictionaries
        with open(self.dic_anet_file) as f:
            self.comm.dic_anet = json.load(f)

        # Get detections to index
        self.comm.dtoi = {w: i+1 for w,
                          i in self.comm.dic_anet['wtod'].items()}
        self.comm.itod = {i: w for w, i in self.comm.dtoi.items()}
        self.comm.itow = self.comm.dic_anet['ix_to_word']
        self.comm.wtoi = {w: i for i, w in self.comm.itow.items()}

        self.comm.vocab_size = len(self.comm.itow) + 1
        self.comm.detect_size = len(self.comm.itod)

    def __len__(self):
        return len(self.annots)  #
        # return 50

    def __getitem__(self, idx: int):
        return self.itemgetter(idx)

    def pad_words_with_vocab(
            self, out_list,
            voc=None, pad_len=-1, defm=[1]):
        """
        Input is a list.
        If curr_len < pad_len: pad remaining with default value
        Instead, if cur_len > pad_len: trim the input
        """
        curr_len = len(out_list)
        if pad_len == -1 or curr_len == pad_len:
            return out_list
        else:
            if curr_len > pad_len:
                return out_list[:pad_len]
            else:
                if voc is not None and hasattr(voc, 'itos'):
                    assert voc.itos[1] == '<pad>'
                out_list += defm * (pad_len - curr_len)
                return out_list

    def get_props(self, index: int):
        """
        Returns the padded proposals, padded mask, number of proposals
        by reading the h5 files
        """
        num_proposals = int(self.num_proposals[index])
        label_proposals = self.label_proposals[index]
        proposals = copy.deepcopy(label_proposals[:num_proposals, :])

        # proposal mask to filter out low-confidence proposals or backgrounds
        # mask is 1 if proposal is included
        pnt_mask = (proposals[:, 6] >= self.prop_thresh)
        if self.exclude_bgd_det:
            pnt_mask &= (proposals[:, 5] != 0)

        num_props = min(proposals.shape[0], self.max_proposals)

        padded_props = self.pad_words_with_vocab(
            proposals.tolist(), pad_len=self.max_proposals, defm=[[0]*7])
        padded_mask = self.pad_words_with_vocab(
            pnt_mask.tolist(), pad_len=self.max_proposals, defm=[0])
        return np.array(padded_props), np.array(padded_mask), num_props

    def get_features(self, vid_seg_id: str, num_proposals: int, props):
        """
        Returns the region features, rgb-motion features
        """

        vid_id_ix, seg_id_ix = vid_seg_id.split('_segment_')
        seg_id_ix = str(int(seg_id_ix))

        region_feature_file = self.feature_root / f'{vid_seg_id}.npy'
        region_feature = np.load(region_feature_file)
        region_feature = region_feature.reshape(
            -1,
            region_feature.shape[2]
        ).copy()
        assert(num_proposals == region_feature.shape[0])
        if self.cfg.misc.add_prop_to_region:
            region_feature = np.concatenate(
                [region_feature, props[:num_proposals, :5]],
                axis=1
            )

        # load the frame-wise segment feature
        seg_rgb_file = self.seg_feature_root / f'{vid_id_ix[2:]}_resnet.npy'
        seg_motion_file = self.seg_feature_root / f'{vid_id_ix[2:]}_bn.npy'

        assert seg_rgb_file.exists() and seg_motion_file.exists()

        seg_rgb_feature = np.load(seg_rgb_file)
        seg_motion_feature = np.load(seg_motion_file)
        seg_feature_raw = np.concatenate(
            (seg_rgb_feature, seg_motion_feature), axis=1)

        return region_feature, seg_feature_raw

    def get_frm_mask(self, proposals, gt_bboxs):
        """
        1 where proposals and gt box don't match
        0 where they match
        We are basically matching the frame indices,
        that is 1 where they belong to different frames
        0 where they belong to same frame.

        In mdl_bbox_utils.py -> bbox_overlaps_batch
        frm_mask ~= frm_mask is used.
        (We have been tricked, we have been backstabbed,
        quite possibly bamboozled)
        """
        # proposals: num_pps
        # gt_bboxs: num_box
        num_pps = proposals.shape[0]
        num_box = gt_bboxs.shape[0]
        return (np.tile(proposals.reshape(-1, 1), (1, num_box)) != np.tile(
            gt_bboxs, (num_pps, 1)))

    def get_seg_feat_for_frms(self, seg_feats, timestamps, duration, idx=None):
        """
        Given seg features of shape num_frms x 3072
        converts to 10 x 3072
        Here 10 is the number of frames used by the mdl
        timestamps contains the start and end time of the clip
        duration is the total length of the video
        note that end-st != dur, since one is for the clip
        other is for the video

        Additionally returns average over the timestamps
        """
        # ctx is the context of the optical flow used
        # 10 means 5 seconds previous, to 5 seconds after
        # This is because optical flow is calculated at
        # 2fps
        ctx = self.cfg.misc.ctx_for_seg_feats
        if timestamps[0] > timestamps[1]:
            # something is wrong in AnetCaptions dataset
            # since only 2 have problems, ignore
            # print(idx, 'why')
            timestamps = timestamps[1], timestamps[0]
        st_time = timestamps[0]
        end_time = timestamps[1]
        duration_clip = end_time - st_time

        num_frms = seg_feats.shape[0]
        frm_ind = np.arange(0, 10)
        frm_time = st_time + (duration_clip / 10) * (frm_ind + 0.5)
        # *2 because of sampling at 2fps
        frm_index_in_seg_feat = np.minimum(np.maximum(
            (frm_time*2).astype(np.int_)-1, 0), num_frms-1)

        st_indices = np.maximum(frm_index_in_seg_feat - ctx - 1, 0)
        end_indices = np.minimum(frm_index_in_seg_feat + ctx + 1, num_frms)

        if not st_indices[0] == end_indices[-1]:
            try:
                seg_feats_frms_glob = seg_feats[st_indices[0]:end_indices[-1]].mean(
                    axis=0)
            except RuntimeWarning:
                import pdb
                pdb.set_trace()
        else:
            print(f'clip duration: {duration_clip}')
            seg_feats_frms_glob = seg_feats[st_indices[0]]

        assert np.all(end_indices - st_indices > 0)
        try:
            if ctx != 0:
                seg_feats_frms = np.vstack([
                    seg_feats[sti:endi, :].mean(axis=0)
                    for sti, endi in zip(st_indices, end_indices)])
            else:
                seg_feats_frms = seg_feats[frm_index_in_seg_feat]
        except RuntimeWarning:
            import pdb
            pdb.set_trace()
            pass
        return seg_feats_frms, seg_feats_frms_glob

    def get_gt_annots(self, caption_dct: Dict, idx: int):
        gt_bboxs = torch.tensor(caption_dct['bbox']).float()
        gt_frms = torch.tensor(caption_dct['frm_idx']).unsqueeze(-1).float()
        assert len(gt_bboxs) == len(gt_frms)
        num_box = len(gt_bboxs)
        gt_bboxs_t = torch.cat([gt_bboxs, gt_frms], dim=-1)

        padded_gt_bboxs = self.pad_words_with_vocab(
            gt_bboxs_t.tolist(),
            pad_len=self.max_gt_box,
            defm=[[0]*5]
        )
        padded_gt_bboxs_mask_list = [1] * num_box
        padded_gt_box_mask = self.pad_words_with_vocab(
            padded_gt_bboxs_mask_list,
            pad_len=self.max_gt_box,
            defm=[0]
        )
        return {
            'padded_gt_bboxs': np.array(padded_gt_bboxs),
            'padded_gt_box_mask': np.array(padded_gt_box_mask),
            'num_box': num_box
        }

    def simple_item_getter(self, idx: int):
        """
        Basically, this returns stuff for the
        vid_seg_id obtained from the idx
        """
        row = self.annots.iloc[idx]

        vid_id = row['vid_id']
        seg_id = str(row['seg_id'])
        vid_seg_id = row['id']
        ix = row['Index']

        # Get the padded proposals, proposal masks and the number of proposals
        padded_props, pad_pnt_mask, num_props = self.get_props(ix)

        # Get the region features and the segment features
        # Region features are for spatial stuff
        # Segment features are for temporal stuff
        region_feature, seg_feature_raw = self.get_features(
            vid_seg_id, num_proposals=num_props, props=padded_props
        )

        # not accurate, with minor misalignments
        # Get the time stamp information for each segment
        timestamps = self.raw_caption[vid_id]['timestamps'][int(seg_id)]

        # Get the durations for each time stamp
        dur = self.raw_caption[vid_id]['duration']

        # Get the number of frames in the segment
        num_frm = seg_feature_raw.shape[0]

        # basically time stamps.
        # Not really used, kept for legacy reasons
        sample_idx = np.array(
            [
                np.round(num_frm*timestamps[0]*1./dur),
                np.round(num_frm*timestamps[1]*1./dur)
            ]
        )

        sample_idx = np.clip(np.round(sample_idx), 0,
                             self.t_attn_size).astype(int)

        # Get segment features based on the number of frames used
        seg_feature = np.zeros((self.t_attn_size, seg_feature_raw.shape[1]))
        seg_feature[:min(self.t_attn_size, num_frm)
                    ] = seg_feature_raw[:self.t_attn_size]

        # gives both local and global features.
        # In model can choose either one
        seg_feature_for_frms, seg_feature_for_frms_glob = (
            self.get_seg_feat_for_frms(
                seg_feature_raw, timestamps, dur, idx)
        )

        # get gt annotations
        # Get the a AE annotations
        caption_dct = self.anet_ent_captions[vid_id]['segments'][seg_id]

        # get the groundtruth_box annotations
        gt_annot_dict = self.get_gt_annots(caption_dct, idx)
        # extract the padded gt boxes
        pad_gt_bboxs = gt_annot_dict['padded_gt_bboxs']
        # store the number of gt boxes
        num_box = gt_annot_dict['num_box']

        # frame mask is NxM matrix of which proposals
        # lie in the same frame of groundtruth
        frm_mask = self.get_frm_mask(
            padded_props[:num_props, 4], pad_gt_bboxs[:num_box, 4]
        )
        # pad it
        pad_frm_mask = np.ones((self.max_proposals, self.max_gt_box))
        pad_frm_mask[:num_props, :num_box] = frm_mask

        pad_pnt_mask = torch.tensor(pad_pnt_mask).long()

        # pad region features
        pad_region_feature = np.zeros(
            (self.max_proposals, region_feature.shape[1]))
        pad_region_feature[:num_props] = region_feature[:num_props]

        out_dict = {
            # segment features
            'seg_feature': torch.from_numpy(seg_feature).float(),
            # local segment features
            'seg_feature_for_frms': torch.from_numpy(
                seg_feature_for_frms).float(),
            # global segment features
            'seg_feature_for_frms_glob': torch.from_numpy(
                seg_feature_for_frms_glob).float(),
            # number of proposals
            'num_props': torch.tensor(num_props).long(),
            # number of groundtruth boxes
            'num_box': torch.tensor(num_box).long(),
            # padded proposals
            'pad_proposals': torch.tensor(padded_props).float(),
            # padded groundtruth boxes
            'pad_gt_bboxs': torch.tensor(pad_gt_bboxs).float(),
            # padded groundtruth mask, not used, kept for legacy
            'pad_gt_box_mask': torch.tensor(
                gt_annot_dict['padded_gt_box_mask']).byte(),
            # segment id, not used, kept for legacy
            'seg_id': torch.tensor(int(seg_id)).long(),
            # idx, ann_idx are same correspond to
            # it is the index of vid_seg in the ann_file
            'idx': torch.tensor(idx).long(),
            'ann_idx': torch.tensor(idx).long(),
            # padded region features
            'pad_region_feature': torch.tensor(pad_region_feature).float(),
            # padded frame mask
            'pad_frm_mask': torch.tensor(pad_frm_mask).byte(),
            # padded proposal mask
            'pad_pnt_mask': pad_pnt_mask.byte(),
            # sample number, not used, legacy
            'sample_idx': torch.tensor(sample_idx).long(),
        }

        return out_dict


class AnetVerbDataset(AnetEntDataset):
    """
    The basic ASRL dataset.
    All outputs for one query
    """

    def fix_via_ast(self, df: DF):
        """
        ASRL csv has columns containing list
        which are read as strings.
        so [1,2] is read as "[1,2]"
        This is fixed using ast.literal_eval
        which would convert the string to a list/dict
        depending on the input
        """
        for k in df.columns:
            first_word = df.iloc[0][k]
            if isinstance(first_word, str) and (first_word[0] in '[{'):
                df[k] = df[k].apply(
                    lambda x: ast.literal_eval(x))
        return df

    def __len__(self):
        return len(self.srl_annots)

    def pidx2list(self, pidx):
        """
        Converts process_idx2 to single list
        Just a convenience function required
        because some places it is list,
        some places it isn't
        """
        lst = []
        for p1 in pidx:
            if not isinstance(p1, list):
                p1 = [p1]
            for p2 in p1:
                if not isinstance(p2, list):
                    p2 = [p2]
                for p3 in p2:
                    assert not isinstance(p3, list)
                    lst.append(p3)
        return lst

    def get_srl_anns(self, srl_row, out=None):
        """
        To output dictionary of whatever srl needs
        1. tags
        2. args with st, end ixs
        3. box ind matching

        This is a pretty detailed function, and
        really requires patience to understand.
        I know I know. Forgive me.
        """
        srl_row = copy.deepcopy(srl_row)

        def word_to_int_vocab(words, voc, pad_len=-1):
            """
            A convenience function to convert words
            into their indices given a vocab.
            Using Anet Vocab only.
            Optionally, pad answers as well
            """
            out_list = []
            if hasattr(voc, 'stoi'):
                vocs = voc.stoi
            else:
                vocs = voc
            for w in words:
                if w in vocs:
                    out_list.append(int(vocs[w]))
                else:
                    if hasattr(voc, 'UNK'):
                        unk_word = voc.UNK
                    else:
                        unk_word = 'UNK'
                    out_list.append(int(vocs[unk_word]))
            curr_len = len(out_list)
            return self.pad_words_with_vocab(out_list,
                                             voc, pad_len=pad_len), curr_len

        # srl args to worry about
        vis_set = self.cfg.ds.include_srl_args

        # want to get the arguments and the word indices
        # req_pat_ix: [['ARG0', [0,1,2,3]], ...]
        # srl_args = ['ARG0', 'V', ...]
        # srl_words_inds = [[0,1,2,3], ...]
        srl_args, srl_words_inds = [list(t) for t in zip(*srl_row.req_pat_ix)]

        # simple mask to care only about those in srl_set
        # also pad them
        srl_args_visual_msk = self.pad_words_with_vocab(
            [s in vis_set for s in srl_args],
            pad_len=self.srl_arg_len, defm=[0]
        )

        # get the words from their indices
        # convert to words
        # if original sentence is 'A child playing tennis'
        # [[0,1], ...] -> [['A', 'child'],...]
        srl_arg_words = [[srl_row.words[ix]
                          for ix in y] for y in srl_words_inds]

        # Tags are converted via tag vocab
        # not used, kept for legacy
        tag_seq = [srl_row.tags[ix] for y in srl_words_inds for ix in y]
        tag_word_ind, _ = word_to_int_vocab(
            # srl_row.tags,
            tag_seq,
            self.arg_vocab['arg_tag_vocab'],
            pad_len=self.seq_length
        )

        # Argument Names (ARG0/V/) are  converted to indices
        # Max num of arguments is kept to be self.srl_arg_len
        # very few cases
        assert 'V' in srl_args
        verb_ind_in_srl = srl_args.index('V')
        if not verb_ind_in_srl <= self.srl_arg_len - 1:
            verb_ind_in_srl = 0

        # Use the argument vocab created earlier
        # convert the arguments to indices using the vocab
        srl_arg_inds, srl_arg_len = word_to_int_vocab(
            srl_args, self.arg_vocab['arg_vocab'],
            pad_len=self.srl_arg_len
        )

        if srl_arg_len > self.srl_arg_len:
            srl_arg_len = self.srl_arg_len

        # defm: is the default matrix to be used
        defm = tuple([[1] * self.seq_length, 0])
        # convert the words to their indices using the vocab
        # for every argument
        # the vocab here is self.comm.wtoi obtained from AE
        srl_arg_words_ind_length = self.pad_words_with_vocab(
            [word_to_int_vocab(
                srl_arg_w, self.comm.wtoi, pad_len=self.seq_length) for
                srl_arg_w in srl_arg_words],
            pad_len=self.srl_arg_len, defm=[defm]
        )

        # Unzip to get the word indices and their lengths for
        # each argument separately
        srl_arg_words_ind, srl_arg_words_length = [
            list(t) for t in zip(*srl_arg_words_ind_length)]

        # This is used to convert
        # [[ARG0: w1,w2], [ARG1: w5,..]] ->
        # [w1,w2,w5]
        # Basically, convert
        # [0] 0,1 -> 0,1
        # [1] 0,1 -> 40, 41
        # and so on
        # Finally, use this with index_select
        # in the mdl part
        srl_arg_word_list = [
            torch.arange(0+st, 0+st+wlen)
            for st, wlen in zip(
                range(
                    0,
                    self.seq_length*self.srl_arg_len,
                    self.seq_length), srl_arg_words_length)
        ]

        # Concat above list
        srl_arg_words_list = torch.cat(srl_arg_word_list, dim=0).tolist()
        # Create the mask to be used with index select
        srl_arg_words_mask = self.pad_words_with_vocab(
            srl_arg_words_list, pad_len=self.seq_length, defm=[-1]
        )

        # Get the start and end positions
        # these are used to retrieve
        # LSTM outputs of the sentence
        # to the argument vectors
        srl_arg_word_list_tmp = [
            0] + torch.cumsum(
                torch.tensor(srl_arg_words_length),
                dim=0).tolist()

        srl_arg_words_capture = [
            (min(x, self.seq_length-1), min(y-1, self.seq_length-1))
            if wlen > 0 else (0, 0)
            for x, y, wlen in zip(
                srl_arg_word_list_tmp[:-1],
                srl_arg_word_list_tmp[1:],
                srl_arg_words_length
            )
        ]

        # This is used to retrieve in argument form from
        # the sentence form
        # Basically, [w1,w2,w5] -> [[ARG0: w1,w2], [ARG1: w5]]
        # Restrict to max len because scatter is used later
        srl_arg_words_map_inv = [
            y_ix for y_ix, y in enumerate(
                srl_words_inds[:self.srl_arg_len]) for ix in y]

        # Also, pad it
        srl_arg_words_map_inv = self.pad_words_with_vocab(
            srl_arg_words_map_inv,
            pad_len=self.seq_length,
            defm=[0]
        )

        # The following creates a binary mask for the sequence_length
        # [1] * seq_cnt for every ARG row
        # This is applied to the scatter output
        defm = [[0] * self.seq_length]
        seq_cnt = sum(srl_arg_words_length)
        srl_arg_words_binary_mask = self.pad_words_with_vocab(
            [self.pad_words_with_vocab(
                [1]*seq_cnt, pad_len=self.seq_length, defm=[0])
             for srl_arg_w in srl_arg_words],
            pad_len=self.srl_arg_len, defm=defm)

        # Get the set of visual words
        vis_idxs_set = set(self.pidx2list(srl_row.process_idx2))
        # Create a map for getting which are the visual words
        srl_arg_words_conc_ix = [ix for y in srl_words_inds for ix in y]
        # Create the binary mask
        srl_vis_words_binary_mask = self.pad_words_with_vocab(
            [1 if srl_vw1 in vis_idxs_set else 0
             for srl_vw1 in srl_arg_words_conc_ix],
            pad_len=self.seq_length, defm=[0])

        # The following are used to map the gt boxes
        # The first is the srl argument, followed by an
        # indicator wheather the box is valid or not
        # third is if valid which boxes to look at
        srl_args, srl_arg_box_indicator, srl_arg_box_inds = [
            list(t) for t in zip(*srl_row.req_cls_pats_mask)
        ]

        # srl boxes, and their lengths are stored in a list
        srl_boxes = []
        srl_boxes_lens = []
        for s1_ind, s1 in enumerate(srl_arg_box_inds):
            mult = min(
                len(s1),
                self.box_per_srl_arg
            ) if srl_arg_box_indicator[s1_ind] == 1 else 0

            s11 = [x if x_ix < self.box_per_srl_arg else 0 for x_ix,
                   x in enumerate(s1)]
            srl_boxes.append(self.pad_words_with_vocab(
                s11, pad_len=self.box_per_srl_arg, defm=[0]))
            srl_boxes_lens.append(self.pad_words_with_vocab(
                [1]*mult, pad_len=self.box_per_srl_arg, defm=[0]))

        # They are then padded
        srl_boxes = self.pad_words_with_vocab(
            srl_boxes,
            pad_len=self.srl_arg_len,
            defm=[[0]*self.box_per_srl_arg]
        )
        srl_boxes_lens = self.pad_words_with_vocab(
            srl_boxes_lens,
            pad_len=self.srl_arg_len,
            defm=[[0]*self.box_per_srl_arg]
        )

        # An indicator wheather the boxes are valid
        srl_arg_boxes_indicator = self.pad_words_with_vocab(
            srl_arg_box_indicator, pad_len=self.srl_arg_len, defm=[0])

        out_dict = {
            # Tags are indexed (B-V -> 4)
            'srl_tag_word_ind': torch.tensor(tag_word_ind).long(),
            # Tag word len available elsewhere, hence removed
            # 'tag_word_len': torch.tensor(tag_word_len).long(),
            # 1 if arg is in ARG1-2/LOC else 0
            'srl_args_visual_msk': torch.tensor(srl_args_visual_msk).long(),
            # ARGs are indexed (ARG0 -> 4, V -> 2)
            'srl_arg_inds': torch.tensor(srl_arg_inds).long(),
            # How many args are considered (ARG0, V,ARG1, ARGM), would be 4
            'srl_arg_len': torch.tensor(srl_arg_len).long(),
            # the above but in mask format
            'srl_arg_inds_msk': torch.tensor(
                [1] * srl_arg_len + [0]*(self.srl_arg_len - srl_arg_len)
            ).long(),
            # Where the verb is located, in prev eg, it would be 1
            'verb_ind_in_srl': torch.tensor(verb_ind_in_srl).long(),
            # num_srl_args x seq_len: for each srl_arg, what is the seq
            # so ARG0: The woman -> [[1946, 4307, ...],...]
            'srl_arg_words_ind': torch.tensor(srl_arg_words_ind).long(),
            # The corresponding lengths of each num_srl
            'srl_arg_words_length': torch.tensor(srl_arg_words_length).long(),
            # num_srl_args x seq_len, 1s upto the seq_len of the whole
            # srl_sent: This is used in scatter operation
            'srl_arg_words_binary_mask': torch.tensor(
                srl_arg_words_binary_mask).long(),
            # Similar to previous, but 1s only at places
            # which are visual words. Used for scatter + GVD
            'srl_vis_words_binary_mask': torch.tensor(
                srl_vis_words_binary_mask).long(),
            # seq_len, but contains in the indices to be gathered
            # from num_srl_args x seq_len -> num_srl_args*seq_len
            # via index_select
            'srl_arg_word_mask': torch.tensor(srl_arg_words_mask).long(),
            # seq_len basically
            'srl_arg_word_mask_len': torch.tensor(min(sum(
                srl_arg_words_length), self.seq_length)).long(),
            # containing start and end points of the words to be collected
            'srl_arg_words_capture': torch.tensor(srl_arg_words_capture).long(),
            # used scatter + GVD
            'srl_arg_words_map_inv': torch.tensor(srl_arg_words_map_inv).long(),
            # box indices in gt boxes
            'srl_boxes': torch.tensor(srl_boxes).long(),
            # mask on which of them to choose
            'srl_boxes_lens': torch.tensor(srl_boxes_lens).long(),
            'srl_arg_boxes_mask': torch.tensor(srl_arg_boxes_indicator).long()
        }
        return out_dict

    def collate_dict_list(self, dict_list, pad_len=None):
        """
        Convert List[Dict[key, val]] -> Dict[key, List[val]]
        Also, pad so that can obtain Dict[key, tensor]
        """
        out_dict = {}
        keys = list(dict_list[0].keys())
        num_dl = len(dict_list)
        if pad_len is None:
            pad_len = self.max_srl_in_sent
        for k in keys:
            dl_list = [dl[k] for dl in dict_list]
            dl_list_pad = self.pad_words_with_vocab(
                dl_list,
                pad_len=pad_len, defm=[dl_list[0]])
            out_dict[k] = torch.stack(dl_list_pad)
        return out_dict, num_dl

    def sent_item_getter(self, idx):
        """
        get vidseg at a time, multiple verbs
        Basically, input is a vid_seg, which may contain
        multiple verbs.
        No longer used, kept for legacy
        """

        ann_ind, srl_rows = self.srl_annots[idx]
        out = self.simple_item_getter(ann_ind)
        out_dict_list = [self.get_srl_anns(srl_rows.iloc[ix], out)
                         for ix in range(len(srl_rows))]
        srl_row_indices = self.pad_words_with_vocab(
            srl_rows.index.tolist(),
            pad_len=self.max_srl_in_sent)
        out_dict, num_verbs = self.collate_dict_list(out_dict_list)
        out_dict['num_verbs'] = torch.tensor(num_verbs).long()
        out_dict['ann_idx'] = torch.tensor(ann_ind).long()
        out_dict['sent_idx'] = torch.tensor(idx).long()
        out_dict['srl_verb_idxs'] = torch.tensor(srl_row_indices).long()
        out.update(out_dict)
        return out

    def get_for_one_verb(self, srl_row, idx, out=None):
        """
        One ASRL index, not used, kept for legacy
        """
        out_dict_list = [self.get_srl_anns(srl_row, out)]
        out_dict, num_verbs = self.collate_dict_list(out_dict_list)
        out_dict['num_verbs'] = torch.tensor(num_verbs).long()
        out_dict['ann_idx'] = torch.tensor(srl_row.ann_ind).long()
        out_dict['sent_idx'] = torch.tensor(idx).long()
        out_dict['srl_verb_idxs'] = torch.tensor([idx]).long()
        return out_dict

    def verb_item_getter(self, idx):
        """
        get verb items, one at a time
        kept for legacy
        """
        srl_row = self.srl_annots.loc[idx]
        out = self.simple_item_getter(srl_row.ann_ind)
        out_dict = self.get_for_one_verb(srl_row, idx, out)
        out.update(out_dict)
        return out


class AV_CS:
    """
    Basically performs CS with SEP/TEMP/SPAT
    It is kept as a separate class
    This allows for modularity, and one could replace
    the parent dataset class for a different dataset
    """

    def __len__(self):
        return len(self.srl_annots)

    def after_init(self):
        """
        Select the SRL annotation file to choose
        As well as the dictionary for CS
        """
        if self.split_type == 'train':
            srl_annot_file = self.cfg.ds.trn_ds4_inds
            arg_dict_file = self.cfg.ds.trn_ds4_dicts
        elif self.split_type == 'valid' or self.split_type == 'test':
            srl_annot_file = self.cfg.ds.val_ds4_inds
            arg_dict_file = self.cfg.ds.val_ds4_dicts
        else:
            raise NotImplementedError

        # Read the file
        self.srl_annots = pd.read_csv(srl_annot_file)
        assert hasattr(self, 'srl_annots')

        # Convert columns to List/Dict
        self.srl_annots = self.fix_via_ast(self.srl_annots)

        # Open the arg dict for CS
        with open(arg_dict_file) as f:
            self.arg_dicts = json.load(f)

        # for now, we only consider the case
        # with one verb at a time
        self.max_srl_in_sent = 1

        # In training allow, for CS, Random
        # or CS+Random
        # The last one doesn't make sense in Val/Test
        if self.split_type == 'train':
            self.sample_type = self.cfg.ds.trn_sample
            assert self.sample_type in set(['ds4', 'random', 'ds4_random'])
        elif self.split_type == 'valid' or self.split_type == 'test':
            self.sample_type = self.cfg.ds.val_sample
            assert self.sample_type in set(['ds4', 'random'])
        else:
            raise NotImplementedError

        # Use sample type to decide which functions to use
        if self.sample_type == 'random':
            self.more_idx_collector = getattr(self, 'get_random_more_idx')
        elif self.sample_type == 'ds4':
            self.more_idx_collector = getattr(self, 'get_cs_more_idxs')
        elif self.sample_type == 'ds4_random':
            self.more_idx_collector = getattr(
                self, 'get_cs_and_random_more_idx')
        else:
            raise NotImplementedError

        # Number of Videos to Use for CS
        if self.split_type == 'train':
            nvids_sample = self.cfg.ds.trn_num_vid_sample
        elif self.split_type in set(['valid', 'test']):
            nvids_sample = self.cfg.ds.val_num_vid_sample
        else:
            raise NotImplementedError

        # set number of videos to use
        self.cs_nvids_sample = nvids_sample
        # itemcollector basically collects
        # nvid samples
        self.itemcollector = getattr(
            self, 'verb_item_getter_nvid'
        )

        # depending on conc_type choose the
        # __getitem__ function
        # append_everywhere is only used for SEP
        # which appends the lang stuff to each sample
        # this makes the code cleaner
        # if svsq, then do same as sep,
        # and set nvids_sample = 1
        if self.cfg.ds.conc_type == 'spat':
            self.itemgetter = getattr(
                self, 'verb_item_getter_SPAT')
            self.append_everywhere = False
        elif self.cfg.ds.conc_type == 'temp':
            self.itemgetter = getattr(
                self, 'verb_item_getter_TEMP')
            self.append_everywhere = False
        elif self.cfg.ds.conc_type == 'sep':
            self.itemgetter = getattr(
                self, 'verb_item_getter_SEP')
            self.append_everywhere = True
        elif self.cfg.ds.conc_type == 'svsq':
            self.itemgetter = getattr(
                self, 'verb_item_getter_SEP')
            self.append_everywhere = True
            self.cs_nvids_sample = 1
        else:
            raise NotImplementedError

        # Whether to shuffle among the four screens
        # Has to be True. Keep false only for debugging
        self.ds4_shuffle = self.cfg.ds.cs_shuffle

        # open the vocab files for args
        with open(self.cfg.ds.arg_vocab_file, 'rb') as f:
            self.arg_vocab = pickle.load(f)

        # set the max number of SRLs
        # ARG0, V, ARG1 => 3 SRLs
        self.srl_arg_len = self.cfg.misc.srl_arg_length
        # set the max number of boxes for each SRL
        # ARG0: four people => 4 boxes
        self.box_per_srl_arg = self.cfg.misc.box_per_srl_arg

    def get_cs_and_random_more_idx(self, idx):
        """
        Either choose at random or
        choose via CS with uniform probability
        """
        if np.random.random() < 0.5:
            return self.get_random_more_idx(idx)
        else:
            return self.get_cs_more_idxs(idx)

    def get_random_more_idx(self, idx):
        """
        Returns set of random idxs
        """
        if self.split_type == 'train':
            # for train, generate this list at runtime
            more_idxs, _ = create_random_list(
                self.cfg,
                self.srl_annots,
                idx
            )
            if len(more_idxs) > self.cs_nvids_sample - 1:
                more_idxs_new_keys = np.random.choice(
                    list(more_idxs.keys()),
                    min(len(more_idxs), self.cs_nvids_sample-1),
                    replace=False
                )
                more_idxs = {k: more_idxs[k] for k in more_idxs_new_keys}

        elif self.split_type == 'valid' or self.split_type == 'test':
            # for valid/test use pre-generated ones
            # obtain predefined idxs
            more_idxs = self.srl_annots.RandDS4_Inds.loc[idx]
            if len(more_idxs) > self.cs_nvids_sample - 1:
                more_idxs_new_keys = list(more_idxs.keys())[:min(
                    len(more_idxs), self.cs_nvids_sample-1)]
                more_idxs = {k: more_idxs[k] for k in more_idxs_new_keys}

        return more_idxs

    def get_cs_more_idxs(self, idx):
        """
        Returns the set of idxs for contrastive_sampling
        """
        if self.split_type == 'train':
            more_idxs, _ = create_similar_list(self.cfg, self.arg_dicts,
                                               self.srl_annots, idx)
            if len(more_idxs) > self.cs_nvids_sample - 1:
                more_idxs_new_keys = np.random.choice(
                    list(more_idxs.keys()),
                    min(len(more_idxs), self.cs_nvids_sample-1),
                    replace=False
                )
                more_idxs = {k: more_idxs[k] for k in more_idxs_new_keys}

        elif self.split_type == 'valid' or self.split_type == 'test':
            # obtain predefined idxs
            more_idxs = self.srl_annots.DS4_Inds.loc[idx]
            if len(more_idxs) > self.cs_nvids_sample - 1:
                more_idxs_new_keys = list(more_idxs.keys())[:min(
                    len(more_idxs), self.cs_nvids_sample-1)]
                more_idxs = {k: more_idxs[k] for k in more_idxs_new_keys}

        return more_idxs

    def verb_item_getter(self, idx):
        """
        get verb items, one at a time
        """
        srl_row = self.srl_annots.loc[idx]
        out = self.simple_item_getter(srl_row.ann_ind)
        out_dict = self.get_srl_anns(srl_row, out)
        out_dict['ann_idx'] = torch.tensor(srl_row.ann_ind).long()
        out_dict['sent_idx'] = torch.tensor(idx).long()
        out.update(out_dict)
        return out

    def verb_item_getter_SPAT(self, idx):
        """
        Use Sampled Indices.
        The output is such that we have
        four screens which being played at
        the same time. The goal is to choose
        the correct screen, and ground the
        correct object in that screen.

        Currently, we implement as
        - 4 x region features
        - 4 x temporal features
        However, only one of the four videos
        has the correct answer.

        The way the model sees the input is
        like one single video with 4 screens.
        This is to get away from the problem of
        no prediction score for the whole
        video is generated by the model.
        """
        def reshuffle_boxes(inp_t):
            """
            input would have nvids x nfrms x nppf
            change it to nfrms x nvids x nppf
            """
            n = inp_t.size(0)
            inp_t = inp_t.view(
                n, self.num_frms, self.num_prop_per_frm, *inp_t.shape[2:]
            ).transpose(0, 1).contiguous().view(
                self.num_frms * n * self.num_prop_per_frm, *inp_t.shape[2:]
            )
            return inp_t

        def process_props(
                props, shift=720,
                keepdim=False, reshuffle_box=False
        ):
            """
            props: n x 1000 x 7
            NOTE: assumes props are already resized
            with width=720 and const height (405)
            """
            n, num_props, pdim = props.shape
            delta = torch.arange(n) * shift
            delta = delta.view(n, 1, 1).expand(
                n, num_props, pdim)
            delta_msk = props.new_zeros(*props.shape)
            delta_msk[..., [0, 2]] = 1
            delta = delta.float() * delta_msk.float()

            props_new = props + delta
            if reshuffle_box:
                props_new = reshuffle_boxes(props_new)

            if keepdim:
                return props_new.view(n, num_props, pdim)
            return props_new.view(n*num_props, pdim)

        def process_gt_boxs(gt_boxs, nums):
            gt_box1 = [gtb for gt_box, n1 in zip(
                gt_boxs, nums) for gtb in gt_box[:n1]]
            if len(gt_box1) == 0:
                gt_box1 = [gt_boxs[0, 0]]
            try:
                gt_box1 = torch.stack(gt_box1)
                out = F.pad(
                    gt_box1, (0, 0, 0, self.max_gt_box - len(gt_box1)),
                    mode='constant', value=0
                )

                assert out.shape == (self.max_gt_box, gt_boxs.size(2))
            except:
                ForkedPdb().set_trace()

            return out

        def process_gt_boxs_msk(gt_boxs, nums):
            gt_box1 = [
                gtb for gt_box, n1 in zip(gt_boxs, nums)
                for gtb in gt_box[:n1]]
            if len(gt_box1) == 0:
                gt_box1 = [gt_boxs[0, 0]]
            gt_box1 = torch.stack(gt_box1)
            out = F.pad(
                gt_box1, (0, self.max_gt_box - len(gt_box1)),
                mode='constant', value=0
            )

            return out

        # get sampled vids
        out_dict = self.itemcollector(idx)
        num_cmp = len(out_dict['new_srl_idxs'])

        # need to make all the proposals
        # x axis + n delta
        # note that final num_cmp = 1 for videos
        # for lang side B x num_verbs where
        # num_verbs = 4
        out_dict['num_props'] = out_dict['num_props'].sum(dim=-1)
        # num_cmp becomes 1 because all videos are concatenated
        # to form one video
        out_dict['num_cmp'] = torch.tensor(1)
        # concat props
        out_dict['pad_proposals'] = process_props(
            out_dict['pad_proposals'], keepdim=False, reshuffle_box=True
        )
        # get total number of boxes
        num_box = out_dict['num_box'].sum(dim=-1)

        # concat gt boxes
        out_dict['pad_gt_bboxs'] = process_gt_boxs(
            process_props(
                out_dict['pad_gt_bboxs'], keepdim=True
            ),
            out_dict['num_box']
        )

        # concat gt_box_mask
        out_dict['pad_gt_box_mask'] = process_gt_boxs_msk(
            out_dict['pad_gt_box_mask'], out_dict['num_box']
        )

        # basically, gt boxes were like
        # 4 x 100 with only some of the 100 being gt for each vid
        # now changed to 100, because stacked on top of each other.
        # target_cmp is the correct video where the groundtruth boxes lies
        # set those as true
        tcmp = out_dict['target_cmp'].item()
        nboxes = [0] + out_dict['num_box'].cumsum(dim=0).tolist()
        new_pos = nboxes[tcmp]
        x1 = out_dict['srl_boxes']
        x2 = out_dict['srl_boxes_lens']
        x1[x2 > 0] += new_pos

        out_dict['num_box2'] = out_dict['num_box'].clone()

        out_dict['num_box'] = num_box

        # need to recompute frm_mask
        frm_mask = self.get_frm_mask(
            out_dict['pad_proposals'][:, 4],
            out_dict['pad_gt_bboxs'][:num_box, 4]
        )

        # pad the frm_mask
        pad_frm_mask = np.ones((num_cmp * self.max_proposals, self.max_gt_box))
        pad_frm_mask[:, :num_box] = frm_mask
        out_dict['pad_frm_mask'] = torch.from_numpy(pad_frm_mask).byte()

        # proposal mask has to be reshuffled
        out_dict['pad_pnt_mask'] = reshuffle_boxes(
            out_dict['pad_pnt_mask']
        )
        # region features have to be reshuffled
        out_dict['pad_region_feature'] = reshuffle_boxes(
            out_dict['pad_region_feature']
        )

        # seg features are vid features, so just combine_first_ax
        out_dict['seg_feature'] = combine_first_ax(
            out_dict['seg_feature'], keepdim=False)
        out_dict['seg_feature_for_frms'] = combine_first_ax(
            out_dict['seg_feature_for_frms'].transpose(0, 1).contiguous(),
            keepdim=False
        )
        # not used, kept for legacy
        out_dict['sample_idx'] = combine_first_ax(
            out_dict['sample_idx'], keepdim=False
        )

        return out_dict

    def verb_item_getter_TEMP(self, idx):
        """
        Similar to spatial, but stack in the temporal
        dimension.
        """
        # For temporal stacking: do the following:
        # mostly everything is stacked temporally,
        # only the durations would perhaps change
        # bboxes frame ids would change
        # Caveats: Videos are not of equal length
        # The above is also applicable to spatial
        def process_props(props, shift=10, keepdim=False,
                          reshuffle_box=False):
            """
            props: n x 1000 x 7
            NOTE: may need to resize
            """
            n, num_props, pdim = props.shape
            delta = torch.arange(n) * shift
            delta = delta.view(n, 1, 1).expand(n, num_props, pdim)
            delta_msk = props.new_zeros(*props.shape)
            delta_msk[..., [4]] = 1
            delta = delta.float() * delta_msk.float()

            props_new = props + delta
            # if reshuffle_box:
            #     props_new = props_new.view(
            #         n, self.num_frms, self.num_prop_per_frm, pdim
            #     ).transpose(0, 1).contiguous()

            if keepdim:
                return props_new.view(n, num_props, pdim)
            return props_new.view(n*num_props, pdim)

        def process_gt_boxs(gt_boxs, nums):
            gt_box1 = [
                gtb for gt_box, n1 in zip(gt_boxs, nums)
                for gtb in gt_box[:n1]]
            if len(gt_box1) == 0:
                gt_box1 = [gt_boxs[0, 0]]
            gt_box1 = torch.stack(gt_box1)
            return F.pad(
                gt_box1, (0, 0, 0, self.max_gt_box - len(gt_box1)),
                mode='constant', value=0
            )

        def process_gt_boxs_msk(gt_boxs, nums):
            gt_box1 = [
                gtb for gt_box, n1 in zip(gt_boxs, nums)
                for gtb in gt_box[:n1]]
            if len(gt_box1) == 0:
                gt_box1 = [gt_boxs[0, 0]]
            gt_box1 = torch.stack(gt_box1)
            out = F.pad(
                gt_box1, (0, self.max_gt_box - len(gt_box1)),
                mode='constant', value=0
            )

            return out.unsqueeze(0)

        # Stack proposals in frames

        out_dict = self.itemcollector(idx)
        num_cmp = len(out_dict['new_srl_idxs'])
        # need to make all the proposals
        # x axis + n delta
        # note that final num_cmp = 1 for videos
        # for lang side B x num_verbs where
        # num_verbs = 4
        # num_cmp1 = out_dict['pad_proposals'].size(0)
        num_props = out_dict['num_props'].sum(dim=-1)
        out_dict['num_props'] = num_props
        num_box = out_dict['num_box'].sum(dim=-1)
        out_dict['num_cmp'] = torch.tensor(1)

        out_dict['pad_proposals'] = process_props(
            out_dict['pad_proposals'], keepdim=False)
        # re-do gt boxes

        out_dict['pad_gt_bboxs'] = process_gt_boxs(process_props(
            out_dict['pad_gt_bboxs'], keepdim=True), out_dict['num_box'])

        out_dict['pad_gt_box_mask'] = process_gt_boxs_msk(
            out_dict['pad_gt_box_mask'], out_dict['num_box']
        )

        tcmp = out_dict['target_cmp'].item()
        nboxes = [0] + out_dict['num_box'].cumsum(dim=0).tolist()
        new_pos = nboxes[tcmp]
        x1 = out_dict['srl_boxes']
        x2 = out_dict['srl_boxes_lens']
        x1[x2 > 0] += new_pos

        out_dict['num_box2'] = out_dict['num_box'].clone()
        out_dict['num_box'] = num_box

        frm_mask = self.get_frm_mask(
            out_dict['pad_proposals'][:, 4],
            out_dict['pad_gt_bboxs'][:num_box, 4])
        pad_frm_mask = np.ones((num_cmp * self.max_proposals, self.max_gt_box))
        pad_frm_mask[:, :num_box] = frm_mask

        out_dict['pad_region_feature'] = combine_first_ax(
            out_dict['pad_region_feature'], keepdim=False
        )

        out_dict['pad_frm_mask'] = torch.from_numpy(pad_frm_mask).byte()
        out_dict['pad_pnt_mask'] = combine_first_ax(
            out_dict['pad_pnt_mask'], keepdim=False)

        out_dict['seg_feature'] = combine_first_ax(
            out_dict['seg_feature'], keepdim=False)
        out_dict['seg_feature_for_frms'] = combine_first_ax(
            out_dict['seg_feature_for_frms'], keepdim=False)
        out_dict['sample_idx'] = combine_first_ax(
            out_dict['sample_idx'], keepdim=False
        )

        return out_dict

    def verb_item_getter_SEP(self, idx):
        """
        When we want separate videos
        """
        return self.itemcollector(idx)

    def verb_item_getter_nvid(self, idx):
        """
        Collect the samples
        If SEP, append language to each vid,
        can then directly output
        If others, don't
        SPAT/TEMP concat the videos in their
        own functions.
        """
        def append_to_every_dict(dct_list, new_dct):
            "append a dict to every dict in a list of dicts"
            for dct in dct_list:
                dct.update(new_dct)
            return

        def shuffle_list_from_perm(lst, perm):
            return [lst[ix] for ix in perm]

        # sample idxs
        more_idxs = self.more_idx_collector(idx)

        new_idxs = [idx]
        lemma_verbs = self.srl_annots.lemma_verb
        curr_verb = lemma_verbs.loc[idx]
        verb_cmp = [1]
        verb_list = [curr_verb]

        # some shenanigans for SEP
        # basically need the verb
        # which helps in choosing the
        # correct video
        if self.split_type == 'train':
            cons = 0
            while len(new_idxs) < self.cs_nvids_sample:
                for arg_name, arg_ids in more_idxs.items():
                    if len(new_idxs) < self.cs_nvids_sample:
                        arg_id_to_append = arg_ids[cons]
                        # TODO: should be removable
                        if arg_id_to_append != -1:
                            new_idxs += [arg_id_to_append]
                            new_verb = lemma_verbs.loc[arg_id_to_append]
                            verb_cmp += [int(new_verb == curr_verb)]
                            verb_list += [new_verb]
                cons += 1
        else:
            cons = 0
            for arg_name, arg_ids in more_idxs.items():
                if len(new_idxs) < self.cs_nvids_sample:
                    arg_id_to_append = arg_ids[cons]
                    # TODO: should be removable
                    if arg_id_to_append != -1:
                        new_idxs += [arg_id_to_append]
                        new_verb = lemma_verbs.loc[arg_id_to_append]
                        verb_cmp += [int(new_verb == curr_verb)]
                        verb_list += [new_verb]

        if self.cfg.ds.cs_shuffle:
            simple_permute = torch.randperm(len(new_idxs))
        else:
            simple_permute = torch.arange(len(new_idxs))

        # these are mainly for debugging purposes
        simple_permute_inv = simple_permute.argsort()
        simple_permute = simple_permute.tolist()
        simple_permute_inv = simple_permute_inv.tolist()
        # this is where the correct index lies
        targ_cmp = simple_permute_inv[0]

        new_idxs = shuffle_list_from_perm(new_idxs, simple_permute)
        verb_cmp = shuffle_list_from_perm(verb_cmp, simple_permute)
        verb_list = shuffle_list_from_perm(verb_list, simple_permute)

        ann_id_list = [self.srl_annots.loc[ix].ann_ind for ix in new_idxs]
        new_out_dicts = [self.simple_item_getter(ann_ix) for ann_ix
                         in ann_id_list]

        srl_row = self.srl_annots.loc[idx]
        out_dict_verb_for_idx = self.get_srl_anns(srl_row, new_out_dicts[0])

        # Append to every dict
        # only for SEP
        if self.append_everywhere:
            append_to_every_dict(new_out_dicts, out_dict_verb_for_idx)
            collated_out_dicts, num_cmp = self.collate_dict_list(
                new_out_dicts, pad_len=self.cs_nvids_sample
            )
        else:
            collated_out_dicts, num_cmp = self.collate_dict_list(
                new_out_dicts, pad_len=self.cs_nvids_sample)
            out_dict_verb_for_idx_coll, _ = self.collate_dict_list(
                [out_dict_verb_for_idx], pad_len=1)
            collated_out_dicts.update(out_dict_verb_for_idx_coll)

        new_srl_idxs_pad = self.pad_words_with_vocab(
            new_idxs, pad_len=self.cs_nvids_sample)

        verb_cmp_pad = self.pad_words_with_vocab(
            verb_cmp, pad_len=self.cs_nvids_sample, defm=[0])

        if len(verb_list) > self.cs_nvids_sample:
            verb_list = verb_list[:self.cs_nvids_sample]

        verb_list_np = np.array(verb_list)
        verb_cross_cmp = verb_list_np[:, None] == verb_list_np
        verb_cross_cmp_msk = np.ones(verb_cross_cmp.shape)

        verb_cross_cmp = np.pad(
            verb_cross_cmp,
            (0, self.cs_nvids_sample - len(verb_list)),
            mode='constant', constant_values=0
        )

        verb_cross_cmp_msk = np.pad(
            verb_cross_cmp_msk,
            (0, self.cs_nvids_sample - len(verb_list)),
            mode='constant', constant_values=0
        )

        num_cmp_arr = np.pad(
            np.eye(num_cmp, num_cmp),
            (0, self.cs_nvids_sample - num_cmp),
            mode='constant', constant_values=0
        )

        sp_pad = [ix for ix in range(
            num_cmp, num_cmp + self.cs_nvids_sample-num_cmp)]
        simple_permute = simple_permute + sp_pad
        assert len(simple_permute) == self.cs_nvids_sample
        simple_permute_inv = simple_permute_inv + sp_pad

        out_dict_verb = {}
        # permutation
        out_dict_verb['permute'] = torch.tensor(simple_permute).long()
        # inverse permutation
        out_dict_verb['permute_inv'] = torch.tensor(
            simple_permute_inv).long()
        # target for corr vid
        out_dict_verb['target_cmp'] = torch.tensor(targ_cmp).long()
        # the srl idxs
        out_dict_verb['new_srl_idxs'] = torch.tensor(
            new_srl_idxs_pad).long()
        # sent_idx is a misnomer, it is the idx in
        # asrl_file. used in the evaluation
        out_dict_verb['sent_idx'] = torch.tensor(idx).long()
        # number of videos, would be changed later
        # if SPAT/TEMP
        out_dict_verb['num_cmp'] = torch.tensor(num_cmp).long()
        # mask for number of videos, useful in loss functions
        out_dict_verb['num_cmp_msk'] = torch.tensor(
            [1]*num_cmp + [0] * (self.cs_nvids_sample - num_cmp))
        # 1/0 matrix of which video is corr, only for legacy
        out_dict_verb['num_cross_cmp_msk'] = torch.from_numpy(num_cmp_arr)
        # which verbs are same
        out_dict_verb['verb_cmp'] = torch.tensor(verb_cmp_pad).long()
        # 1/0 matrix of which verbs are same
        out_dict_verb['verb_cross_cmp'] = torch.from_numpy(
            verb_cross_cmp).long()
        # msk to remove padded idx case
        out_dict_verb['verb_cross_cmp_msk'] = torch.from_numpy(
            verb_cross_cmp_msk).long()

        collated_out_dicts.update(out_dict_verb)

        return collated_out_dicts


class Anet_SRL(AV_CS, AnetVerbDataset):
    def __init__(self, cfg: CN, ann_file: Fpath, split_type: str = 'train',
                 comm: Dict = None):
        AnetVerbDataset.__init__(self, cfg, ann_file, split_type, comm)


class BatchCollator:
    """
    Need to redefine this perhaps
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.after_init()

    def after_init(self):
        pass

    def __call__(self, batch):
        out_dict = {}

        # nothing needs to be done
        all_keys = list(batch[0].keys())
        batch_size = len(batch)
        for k in all_keys:
            shape = batch[0][k].shape
            if not all([b[k].shape == shape for b in batch]):
                ForkedPdb().set_trace()
            out_dict[k] = torch.stack(
                    [b[k] for b in batch])
        assert all([len(v) == batch_size for k, v in out_dict.items()])

        return out_dict


def get_data(cfg):
    # Get which dataset to use
    DS = Anet_SRL

    collate_fn = BatchCollator

    # Training file
    trn_ann_file = cfg.ds['trn_ann_file']
    trn_ds = DS(cfg=cfg, ann_file=trn_ann_file,
                split_type='train')
    trn_dl = get_dataloader(cfg, trn_ds, is_train=True,
                            collate_fn=collate_fn)

    # Validation file
    val_ann_file = cfg.ds['val_ann_file']
    val_ds = DS(cfg=cfg, ann_file=val_ann_file,
                split_type='valid')
    val_dl = get_dataloader(cfg, val_ds, is_train=False,
                            collate_fn=collate_fn)

    data = DataWrap(path=cfg.misc.tmp_path, train_dl=trn_dl, valid_dl=val_dl,
                    test_dl=None)
    return data


if __name__ == '__main__':
    cfg = CN(yaml.safe_load(open('./configs/anet_srl_cfg.yml')))
    data = get_data(cfg)

    diter = iter(data.train_dl)
    batch = next(diter)
