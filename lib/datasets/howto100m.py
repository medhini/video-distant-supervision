# Copyright (c) Meta Platforms, Inc. and affiliates. All Rights Reserved.

import os
import random
import torch
import torch.utils.data
from fvcore.common.file_io import PathManager
import numpy as np
import json
import lib.utils.logging as logging

from . import decoder as decoder
from . import utils as utils
from . import video_container as container
from .build import DATASET_REGISTRY
import pickle as pkl

logger = logging.get_logger(__name__)
import re
import pandas as pd
import ffmpeg
import math

os.environ["TOKENIZERS_PARALLELISM"] = "false"


def check_time(s1, e1, s2, e2):
    return max(min(e1, e2) - max(s1, s2), 0)


def get_video(video_path, start, end, number_frames):
    
    cmd = (
        ffmpeg
        .input(video_path, ss=start, t=end-start)
        .filter('fps', fps=math.ceil(number_frames/(end-start)))
    )
    cmd = (
            cmd.filter('scale', 640, 360)
        )
    out, _ = (
        cmd.output('pipe:', format='rawvideo', pix_fmt='rgb24')
        .run(capture_stdout=False, quiet=True)
    )
    
    video = np.frombuffer(out, np.uint8).reshape([-1, 360, 640, 3])
    video2 = torch.tensor(video)
    video2 = temporal_sampling(video2, 0, video2.shape[0], number_frames)
    return video2  

def temporal_sampling(frames, start_idx, end_idx, num_samples):
    """
    Given the start and end frame index, sample num_samples frames between
    the start and end with equal interval.
    Args:
        frames (tensor): a tensor of video frames, dimension is
            `num video frames` x `channel` x `height` x `width`.
        start_idx (int): the index of the start frame.
        end_idx (int): the index of the end frame.
        num_samples (int): number of frames to sample.
    Returns:
        frames (tersor): a tensor of temporal sampled video frames, dimension is
            `num clip frames` x `channel` x `height` x `width`.
    """
    index = torch.linspace(start_idx, end_idx, num_samples)
    index = torch.clamp(index, 0, frames.shape[0] - 1).long()
    frames = torch.index_select(frames, 0, index)
    return frames

def get_start_end_idx(video_size, clip_size, clip_idx, num_clips):
    """
    Sample a clip of size clip_size from a video of size video_size and
    return the indices of the first and last frame of the clip. If clip_idx is
    -1, the clip is randomly sampled, otherwise uniformly split the video to
    num_clips clips, and select the start and end index of clip_idx-th video
    clip.
    Args:
        video_size (int): number of overall frames.
        clip_size (int): size of the clip to sample from the frames.
        clip_idx (int): if clip_idx is -1, perform random jitter sampling. If
            clip_idx is larger than -1, uniformly split the video to num_clips
            clips, and select the start and end index of the clip_idx-th video
            clip.
        num_clips (int): overall number of clips to uniformly sample from the
            given video for testing.
    Returns:
        start_idx (int): the start frame index.
        end_idx (int): the end frame index.
    """
    delta = max(video_size - clip_size, 0)
    if clip_idx == -1:
        # Random temporal sampling.
        start_idx = random.uniform(0, delta)
    else:
        # Uniformly sample the clip with the given index.
        start_idx = delta * clip_idx / num_clips
    end_idx = start_idx + clip_size - 1
    return start_idx, end_idx


@DATASET_REGISTRY.register()
class Howto100m(torch.utils.data.Dataset):
    """
    Kinetics video loader. Construct the Kinetics video loader, then sample
    clips from the videos. For training and validation, a single clip is
    randomly sampled from every video with random cropping, scaling, and
    flipping. For testing, multiple clips are uniformaly sampled from every
    video with uniform cropping. For uniform cropping, we take the left, center,
    and right crop if the width is larger than height, or take top, center, and
    bottom crop if the height is larger than the width.
    """

    def __init__(self, cfg, mode, num_retries=20):
        """
        Construct the Kinetics video loader with a given csv file. The format of
        the csv file is:
        ```
        path_to_video_1 label_1
        path_to_video_2 label_2
        ...
        path_to_video_N label_N
        ```
        Args:
            cfg (CfgNode): configs.
            mode (string): Options includes `train`, `val`, or `test` mode.
                For the train and val mode, the data loader will take data
                from the train or val set, and sample one clip per video.
                For the test mode, the data loader will take data from test set,
                and sample multiple clips per video.
            num_retries (int): number of retries.
        """

        assert mode in [
            "train",
            "val",
            "test",
        ], "Split '{}' not supported for HowTo100M".format(mode)
        self.mode = mode
        import copy

        self.cfg = copy.deepcopy(cfg)
        if hasattr(self.cfg.MODEL, "NUM_SEG") and self.cfg.MODEL.NUM_SEG > 0:
            self.cfg.DATA.NUM_FRAMES *= self.cfg.MODEL.NUM_SEG

        self._video_meta = {}
        self._num_retries = num_retries
        # For training or validation mode, one single clip is sampled from every
        # video. For testing, NUM_ENSEMBLE_VIEWS clips are sampled from every
        # video. For every clip, NUM_SPATIAL_CROPS is cropped spatially from
        # the frames.
        if self.mode in ["train"]:
            self._num_clips = 1
        elif self.mode in ["val"]:
            self._num_clips = 1
        elif self.mode in ["test"]:
            self._num_clips = cfg.TEST.NUM_ENSEMBLE_VIEWS * cfg.TEST.NUM_SPATIAL_CROPS
        self.textind = len(cfg.TRAIN.TEXT) > 0
        self.sample = cfg.TRAIN.TEXT_SAMPLE

        if len(cfg.TRAIN.TEXT) > 0:
            self.caps = cfg.TRAIN.TEXT
            self.labels = None
        else:
            self.labels = None
            self.caps = None
        if len(cfg.TRAIN.TEXT_EMB) > 0:
            self.caps_emb = cfg.TRAIN.TEXT_EMB
        else:
            self.caps_emb = None
        if len(cfg.TRAIN.TEXT) > 0:
            from transformers import AutoTokenizer

            self.tokenizer = (
                AutoTokenizer.from_pretrained(
                    "sentence-transformers/paraphrase-mpnet-base-v2"
                )
                if cfg.MODEL.TEXT_MODEL == "paraphrase-mpnet-base-v2"
                else None
            )
            if hasattr(cfg.MODEL, "MAX_LEN"):
                self.max_len = cfg.MODEL.MAX_LEN
            else:
                self.max_len = 64
            if hasattr(cfg.MODEL, "MIN_LEN"):
                self.min_len = cfg.MODEL.MIN_LEN
            else:
                self.min_len = 0
            self.sample = cfg.TRAIN.TEXT_SAMPLE

        if hasattr(cfg.TRAIN, "EPOCH_MUL"):
            self.em = cfg.TRAIN.EPOCH_MUL
        logger.info("Constructing HowTo100M {}...".format(mode))
        self._construct_loader()

    def _construct_loader(self):
        """
        Construct the video loader.
        """
        path_to_file = os.path.join(
            self.cfg.DATA.PATH_TO_DATA_DIR, "{}.csv".format(self.mode)
        )
        assert PathManager.exists(path_to_file), "{} dir not found".format(path_to_file)

        self._path_to_videos = []
        self._labels = []
        self._durations = []
        self._start = []
        self._end = []
        self._spatial_temporal_idx = []
        tmp = []
        with PathManager.open(path_to_file, "r") as f:
            for clip_idx, path_label in enumerate(f.read().splitlines()):
                assert (
                    len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 3
                    or len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 5
                    or len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 6
                )
                if len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 3:
                    path, label, duration = path_label.split(
                        self.cfg.DATA.PATH_LABEL_SEPARATOR
                    )
                elif len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 5:
                    path, label, duration, start, end = path_label.split(
                        self.cfg.DATA.PATH_LABEL_SEPARATOR
                    )
                else:
                    path, label, duration, start, end, text = path_label.split(
                        self.cfg.DATA.PATH_LABEL_SEPARATOR
                    )
                path = path.split(".")[0]
                for idx in range(self._num_clips):
                    self._path_to_videos.append(
                        os.path.join(self.cfg.DATA.PATH_PREFIX, path)
                    )
                    self._labels.append(int(label))
                    self._durations.append(int(float(duration)))
                    # self._start.append(float(start))
                    # self._end.append(float(end))
                    if len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 3:
                        self._start.append(None)
                        self._end.append(None)
                    else:
                        self._start.append(int(float(start)))
                        self._end.append(int(float(end)))

                    self._spatial_temporal_idx.append(idx)
                    if len(path_label.split(self.cfg.DATA.PATH_LABEL_SEPARATOR)) == 6:
                        tmp.append(text.replace("<>", " "))
                    self._video_meta[clip_idx * self._num_clips + idx] = {}
        assert (
            len(self._path_to_videos) > 0
        ), "Failed to load HowTo100M split {} from {}".format(
            self._split_idx, path_to_file
        )
        logger.info(
            "Constructing kinetics dataloader (size: {}) from {}".format(
                len(self._path_to_videos), path_to_file
            )
        )
        if len(tmp) > 0:
            self.labels = tmp
            print(len(tmp))

    def __getitem__(self, index):
        """
        Given the video index, return the list of frames, label, and video
        index if the video can be fetched and decoded successfully, otherwise
        repeatly find a random video that can be decoded as a replacement.
        Args:
            index (int): the video index provided by the pytorch sampler.
        Returns:
            frames (tensor): the frames of sampled from the video. The dimension
                is `channel` x `num frames` x `height` x `width`.
            label (int): the label of the current video.
            index (int): if the video provided by pytorch sampler can be
                decoded, then return the index of the video. If not, return the
                index of the video replacement that can be decoded.
        """
        short_cycle_idx = None
        # When short cycle is used, input index is a tupple.
        if isinstance(index, tuple):
            index, short_cycle_idx = index
        if hasattr(self, "em") and self.em > 1:
            index = index % len(self._path_to_videos)
        if self.mode in ["train", "val"]:
            # -1 indicates random sampling.
            temporal_sample_index = -1
            spatial_sample_index = -1
            min_scale = self.cfg.DATA.TRAIN_JITTER_SCALES[0]
            max_scale = self.cfg.DATA.TRAIN_JITTER_SCALES[1]
            crop_size = self.cfg.DATA.TRAIN_CROP_SIZE
            if short_cycle_idx in [0, 1]:
                crop_size = int(
                    round(
                        self.cfg.MULTIGRID.SHORT_CYCLE_FACTORS[short_cycle_idx]
                        * self.cfg.MULTIGRID.DEFAULT_S
                    )
                )
            if self.cfg.MULTIGRID.DEFAULT_S > 0:
                # Decreasing the scale is equivalent to using a larger "span"
                # in a sampling grid.
                min_scale = int(
                    round(float(min_scale) * crop_size / self.cfg.MULTIGRID.DEFAULT_S)
                )
        elif self.mode in ["test"]:
            temporal_sample_index = (
                self._spatial_temporal_idx[index] // self.cfg.TEST.NUM_SPATIAL_CROPS
            )
            # spatial_sample_index is in [0, 1, 2]. Corresponding to left,
            # center, or right if width is larger than height, and top, middle,
            # or bottom if height is larger than width.
            spatial_sample_index = (
                (self._spatial_temporal_idx[index] % self.cfg.TEST.NUM_SPATIAL_CROPS)
                if self.cfg.TEST.NUM_SPATIAL_CROPS > 1
                else 1
            )
            min_scale, max_scale, crop_size = (
                [self.cfg.DATA.TEST_CROP_SIZE] * 3
                if self.cfg.TEST.NUM_SPATIAL_CROPS > 1
                else [self.cfg.DATA.TRAIN_JITTER_SCALES[0]] * 2
                + [self.cfg.DATA.TEST_CROP_SIZE]
            )
            # The testing is deterministic and no jitter should be performed.
            # min_scale, max_scale, and crop_size are expect to be the same.
            assert len({min_scale, max_scale}) == 1
        else:
            raise NotImplementedError("Does not support {} mode".format(self.mode))
        sampling_rate = utils.get_random_sampling_rate(
            self.cfg.MULTIGRID.LONG_CYCLE_SAMPLING_RATE,
            self.cfg.DATA.SAMPLING_RATE,
        )
        # Try to decode and sample a clip from a video. If the video can not be
        # decoded, repeatly find a random video replacement that can be decoded.
        for i_try in range(self._num_retries):
            video_container = None
            if not self.cfg.DATA.DECODING_BACKEND == "ffmpeg":
                try:
                    video_container = container.get_video_container(
                        self._path_to_videos[index],
                        self.cfg.DATA_LOADER.ENABLE_MULTI_THREAD_DECODE,
                        self.cfg.DATA.DECODING_BACKEND,
                    )
                except Exception as e:
                    logger.info(
                        "Failed to load video from {} with error {}".format(
                            self._path_to_videos[index], e
                        )
                    )
                # Select a random video if the current video was not able to access.
                if video_container is None:
                    logger.warning(
                        "Failed to meta load video idx {} from {}; trial {}".format(
                            index, self._path_to_videos[index], i_try
                        )
                    )
                    if self.mode not in ["test"] and i_try > self._num_retries // 2:
                        # let's try another one
                        index = random.randint(0, len(self._path_to_videos) - 1)

                    continue

            duration = self._durations[index]
            start = self._start[index]
            end = self._end[index]

            sid = None
            words = None
            text = None
            if (start == None or type(self.caps) == type(" ")) and not (
                self.caps == None and self.labels == None
            ):
                print("Index here: ", index)
                vidid = self._path_to_videos[index].split("/")[-1].split(".")[0]

                cap = pd.read_csv(self.caps + vidid + ".csv")

                # Random ASR sentence gets chosen here
                if start == None:
                    ind = random.randint(0, len(cap) - 1)
                else:
                    temp = []
                    for i in range(len(cap)):
                        temp.append(
                            check_time(
                                start, end, cap["start"].values[i], cap["end"].values[i]
                            )
                        )
                    ind = np.argmax(temp)

                if hasattr(self, "caps_emb") and not self.caps_emb == None:
                    cap_emb = np.load(self.caps_emb + vidid + ".npy")[ind, :]
                else:
                    cap_emb = None
                if hasattr(self, "min_len") and self.min_len > 0:
                    mi = 0
                    q = cap["text"].values[ind]
                    q = q if isinstance(q, str) else " "
                    s = cap["start"].values[ind]
                    e = cap["end"].values[ind]
                    while len(q.split(" ")) < self.min_len:
                        if ind - mi > 0 and isinstance(
                            cap["text"].values[ind - mi], str
                        ):
                            q = cap["text"].values[ind - mi] + " " + q
                            s = cap["start"].values[ind - mi]
                        if ind + mi < len(cap) and isinstance(
                            cap["text"].values[ind + mi], str
                        ):
                            q = q + " " + cap["text"].values[ind + mi]
                            e = cap["end"].values[ind + mi]
                        mi += 1
                        if not ind - mi > 0 and not ind + mi < len(cap):
                            break
                    cap["text"].values[ind] = q
                    cap["start"].values[ind] = s
                    cap["end"].values[ind] = e
                if self.sample < 1 or (not "train" in self.mode):
                    sen = cap["text"].values[ind]
                    if not type(sen) == type(" ") or len(sen) == 0:
                        sen = " "
                    text = self.tokenizer.encode_plus(
                        sen,
                        max_length=self.max_len,
                        padding="max_length",
                        truncation=True,
                        add_special_tokens=True,
                        return_tensors="pt",
                    )

                start, end = cap["start"].values[ind], cap["end"].values[ind]
            if start == None:
                start, end = get_start_end_idx(
                    duration,
                    self.cfg.DATA.FD,
                    temporal_sample_index,
                    self.cfg.TEST.NUM_ENSEMBLE_VIEWS,
                )
            if end - start < self.cfg.DATA.FD - 1:
                start = max((end + start) / 2.0 - self.cfg.DATA.FD / 2.0, 0)
                end = min(start + self.cfg.DATA.FD, duration)
            try:
                if end - start > self.cfg.DATA.NUM_FRAMES and self.cfg.DATA.FD == 0.0:
                    new_end = (end + start) / 2.0 + self.cfg.DATA.NUM_FRAMES / 2.0
                    new_start = (end + start) / 2.0 - self.cfg.DATA.NUM_FRAMES / 2.0
                    start = new_start
                    end = new_end
                elif self.cfg.DATA.FD > 0.0 and self.cfg.DATA.FD < end - start:
                    startb, endb = start, end
                    start, end = get_start_end_idx(
                        end - start,
                        self.cfg.DATA.FD,
                        temporal_sample_index,
                        self.cfg.TEST.NUM_ENSEMBLE_VIEWS,
                    )
                    start += startb
                    end += startb
            except:
                end = end
            if hasattr(self.cfg.DATA, "FIX_END") and self.cfg.DATA.FIX_END:
                start = self._start[index]
                end = self._end[index]
                if self.cfg.DATA.FD < end - start:
                    start, end = get_start_end_idx(
                        end - start,
                        self.cfg.DATA.FD,
                        temporal_sample_index,
                        self.cfg.TEST.NUM_ENSEMBLE_VIEWS,
                    )

            if not self.cfg.DATA.DECODING_BACKEND == "ffmpeg":
                frames = decoder.decode(
                    video_container,
                    sampling_rate,
                    self.cfg.DATA.NUM_FRAMES,
                    temporal_sample_index,
                    self.cfg.TEST.NUM_ENSEMBLE_VIEWS,
                    video_meta=self._video_meta[index],
                    target_fps=self.cfg.DATA.TARGET_FPS,
                    backend=self.cfg.DATA.DECODING_BACKEND,
                    max_spatial_scale=min_scale,
                    duration=duration,
                    start=start,
                    end=end,
                )
            else:
                try:
                    frames = get_video(
                        self._path_to_videos[index],
                        start,
                        end,
                        self.cfg.DATA.NUM_FRAMES,
                    )
                except:
                    frames = None

            # If decoding failed (wrong format, video is too short, and etc),
            # select another video.
            if frames is None:
                logger.warning(
                    "Failed to decode video idx {} from {}; trial {}".format(
                        index, self._path_to_videos[index], i_try
                    )
                )
                if self.mode not in ["test"]:  # and i_try > self._num_retries // 4:
                    # let's try another one
                    index = random.randint(0, len(self._path_to_videos) - 1)
                if self.mode in ["test"] and i_try > self._num_retries // 2:
                    # let's try another one
                    index = random.randint(0, len(self._path_to_videos) - 1)
                continue
            # Perform color normalization.
            frames = utils.tensor_normalize(
                frames, self.cfg.DATA.MEAN, self.cfg.DATA.STD
            )
            # T H W C -> C T H W.
            frames = frames.permute(3, 0, 1, 2)

            # Perform data augmentation.
            frames = utils.spatial_sampling(
                frames,
                spatial_idx=spatial_sample_index,
                min_scale=min_scale,
                max_scale=max_scale,
                crop_size=crop_size,
                random_horizontal_flip=self.cfg.DATA.RANDOM_FLIP,
                inverse_uniform_sampling=self.cfg.DATA.INV_UNIFORM_SAMPLE,
            )

            label = self._labels[index]

            if not self.cfg.MODEL.ARCH in ["vit", "swin3d"]:
                frames = utils.pack_pathway_output(self.cfg, frames)

            if self.textind:
                if text == None:
                    text = {"text": words}
                text["label"] = torch.tensor([1] + [0] * self.sample)
                if hasattr(self, "caps_emb") and not self.caps_emb == None:
                    text["emb"] = cap_emb
                return frames, label, index, text

            return frames, label, index, {}

    def __len__(self):
        """
        Returns:
            (int): the number of videos in the dataset.
        """
        if hasattr(self, "em") and self.em > 1 and self.mode == "train":
            return len(self._path_to_videos) * self.em
        else:
            return len(self._path_to_videos)
