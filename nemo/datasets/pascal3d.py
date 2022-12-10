import os

import BboxTools as bbt
import numpy as np
import torch
import torchvision
from PIL import Image
from torch.utils.data import Dataset

from nemo.utils import construct_class_by_name
from nemo.utils import get_abs_path
from nemo.utils.pascal3d_utils import CATEGORIES


class Pascal3DPlus(Dataset):
    def __init__(
        self,
        data_type,
        category,
        root_path,
        transforms,
        subtypes=None,
        occ_level=0,
        enable_cache=True,
        weighted=True,
        **kwargs,
    ):
        self.data_type = data_type
        self.root_path = get_abs_path(root_path)
        self.category = category
        self.subtypes = subtypes if subtypes is not None else {}
        self.occ_level = occ_level
        self.enable_cache = enable_cache
        self.weighted = weighted
        self.transforms = torchvision.transforms.Compose(
            [construct_class_by_name(**t) for t in transforms]
        )

        if self.category == 'all':
            self.category = CATEGORIES
        if not isinstance(self.category, list):
            self.category = [self.category]
        self.multi_cate = len(self.category) > 1

        self.image_path = os.path.join(self.root_path, data_type, "images")
        self.annotation_path = os.path.join(self.root_path, data_type, "annotations")
        self.list_path = os.path.join(self.root_path, data_type, "lists")

        file_list = []
        for cate in self.category:
            if self.occ_level == 0:
                _list_path = os.path.join(self.list_path, cate)
            else:
                _list_path = os.path.join(self.list_path, f"{cate}FGL{self.occ_level}_BGL{self.occ_level}")

            if cate not in self.subtypes:
                self.subtypes[cate] = [t.split(".")[0] for t in os.listdir(_list_path)]

            file_list += sum(
                (
                    [
                        os.path.join(cate if self.occ_level == 0 else f"{cate}FGL{self.occ_level}_BGL{self.occ_level}", l.strip())
                        for l in open(
                            os.path.join(_list_path, subtype_ + ".txt")
                        ).readlines()
                    ]
                    for subtype_ in self.subtypes[cate]
                ),
                [],
            )
        self.file_list = file_list

        self.cache = {}

    def __len__(self):
        return len(self.file_list)

    def __getitem__(self, item):
        name_img = self.file_list[item]

        if self.enable_cache and name_img in self.cache.keys():
            sample = self.cache[name_img]
        else:
            img = Image.open(os.path.join(self.image_path, f"{name_img}.JPEG"))
            if img.mode != "RGB":
                img = img.convert("RGB")
            annotation_file = np.load(
                os.path.join(self.annotation_path, name_img.split(".")[0] + ".npz"),
                allow_pickle=True,
            )

            if "cropped_kp_list" in annotation_file and "visible" in annotation_file:
                kp = annotation_file["cropped_kp_list"]
                iskpvisible = annotation_file["visible"] == 1

                if self.weighted:
                    iskpvisible = iskpvisible * annotation_file["kp_weights"]

                iskpvisible = np.logical_and(
                    iskpvisible, np.all(kp >= np.zeros_like(kp), axis=1)
                )
                iskpvisible = np.logical_and(
                    iskpvisible, np.all(kp < np.array([img.size[::-1]]), axis=1)
                )

                kp = np.max([np.zeros_like(kp), kp], axis=0)
                kp = np.min(
                    [np.ones_like(kp) * (np.array([img.size[::-1]]) - 1), kp], axis=0
                )
            else:
                kp = np.zeros((100, 2), dtype=np.float32)
                iskpvisible = np.zeros((100,), dtype=np.int32)

            this_name = name_img.split(".")[0]

            try:
                box_obj = bbt.from_numpy(annotation_file["box_obj"])
                obj_mask = np.zeros(box_obj.boundary, dtype=np.float32)
                box_obj.assign(obj_mask, 1)
            except KeyboardInterrupt:
                obj_mask = np.zeros((img.size[1], img.size[0]))

            sample = {
                "this_name": this_name,
                "cad_index": int(annotation_file["cad_index"]),
                "azimuth": float(annotation_file["azimuth"]),
                "elevation": float(annotation_file["elevation"]),
                "theta": float(annotation_file["theta"]),
                "distance": 5.0,
                "bbox": annotation_file["box_obj"],
                "obj_mask": obj_mask,
                "img": img,
                "original_img": np.array(img),
            }
            if not self.multi_cate:
                sample['kp'] = kp.astype(np.float32)
                sample['kpvis'] = iskpvisible.astype(bool)

            if self.enable_cache:
                self.cache[name_img] = sample.copy()

        if self.transforms:
            sample = self.transforms(sample)
        return sample

    def debug(self, item, save_dir=""):
        sample = self.__getitem__(item)
        img = sample["original_img"]
        kp, kpvis = sample["kp"], sample["kpvis"]
        y0, y1, x0, x1, _, _ = sample["bbox"]
        obj_mask = sample["obj_mask"]

        import cv2

        for i in range(len(kp)):
            if kpvis[i]:
                img = cv2.circle(
                    img, (int(kp[i, 1]), int(kp[i, 0])), 2, (255, 0, 0), -1
                )
        img = cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (0, 255, 0), 2)

        gray_img = (img * 0.3).astype(np.uint8)
        gray_img[obj_mask == 1] = img[obj_mask == 1]

        Image.fromarray(gray_img).save(
            os.path.join(save_dir, f'debug_{sample["this_name"]}.png')
        )


class ToTensor:
    def __init__(self):
        self.trans = torchvision.transforms.ToTensor()

    def __call__(self, sample):
        sample["img"] = self.trans(sample["img"])
        if "kpvis" in sample and not isinstance(sample["kpvis"], torch.Tensor):
            sample["kpvis"] = torch.Tensor(sample["kpvis"])
        if "kp" in sample and not isinstance(sample["kp"], torch.Tensor):
            sample["kp"] = torch.Tensor(sample["kp"])
        return sample


class Normalize:
    def __init__(self):
        self.trans = torchvision.transforms.Normalize(
            mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]
        )

    def __call__(self, sample):
        sample["img"] = self.trans(sample["img"])
        return sample


def hflip(sample):
    sample["img"] = torchvision.transforms.functional.hflip(sample["img"])
    if 'kp' in sample:
        sample["kp"][:, 1] = sample["img"].size[0] - sample["kp"][:, 1] - 1
    sample["azimuth"] = np.pi * 2 - sample["azimuth"]
    sample["theta"] = np.pi * 2 - sample["theta"]
    raise NotImplementedError("Horizontal flip is not tested.")

    return sample


class RandomHorizontalFlip:
    def __init__(self):
        self.trans = torchvision.transforms.RandomApply([lambda x: hflip(x)], p=0.5)

    def __call__(self, sample):
        sample = self.trans(sample)
        return sample


class ColorJitter:
    def __init__(self):
        self.trans = torchvision.transforms.ColorJitter(
            brightness=0.2, contrast=0.2, saturation=0.4, hue=0
        )

    def __call__(self, sample):
        sample["img"] = self.trans(sample["img"])
        return sample
