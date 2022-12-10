import torch
import torch.nn as nn

from nemo.models.base_model import BaseModel
from nemo.models.mesh_interpolate_module import MeshInterpolateModule
from nemo.models.solve_pose import pre_compute_kp_coords
from nemo.models.solve_pose import solve_pose
from nemo.utils import center_crop_fun
from nemo.utils import construct_class_by_name
from nemo.utils import get_param_samples
from nemo.utils import load_off
from nemo.utils import normalize_features
from nemo.utils import pose_error
from nemo.utils.pascal3d_utils import IMAGE_SIZES


class NeMo(BaseModel):
    def __init__(
        self,
        cfg,
        cate,
        mode,
        backbone,
        memory_bank,
        num_noise,
        max_group,
        down_sample_rate,
        mesh_path,
        training,
        inference,
        checkpoint=None,
        transforms=[],
        device="cuda:0",
        **kwargs
    ):
        super().__init__(cfg, cate, mode, checkpoint, transforms, ['loss', 'loss_main', 'loss_reg'], device)
        self.net_params = backbone
        self.memory_bank_params = memory_bank
        self.num_noise = num_noise
        self.max_group = max_group
        self.down_sample_rate = down_sample_rate
        self.mesh_path = mesh_path.format(cate) if "{:s}" in mesh_path else mesh_path
        self.training_params = training
        self.inference_params = inference

        self.build()

    def build(self):
        if self.mode == "train":
            self._build_train()
        else:
            self._build_inference()

    def _build_train(self):
        self.n_gpus = torch.cuda.device_count()
        if self.training.separate_bank:
            self.ext_gpu = f"cuda:{self.n_gpus-1}"
        else:
            self.ext_gpu = ""

        net = construct_class_by_name(**self.net_params)
        if self.training.separate_bank:
            self.net = nn.DataParallel(net, device_ids=[i for i in range(self.n_gpus - 1)]).cuda()
        else:
            self.net = nn.DataParallel(net).cuda()

        self.num_verts = load_off(self.mesh_path)[0].shape[0]
        memory_bank = construct_class_by_name(
            **self.memory_bank,
            output_size=self.num_verts+self.num_noise*self.max_group,
            num_pos=self.num_verts,
            num_noise=self.num_noise)
        if self.training.separate_bank:
            self.memory_bank = memory_bank.cuda(self.ext_gpu)
        else:
            self.memory_bank = memory_bank.cuda()

        self.optim = construct_class_by_name(
            **self.training.optimizer, params=self.model.parameters())

    def _build_inference(self):
        self.net = construct_class_by_name(**self.net_params)
        self.net = nn.DataParallel(self.net).to(self.device)
        self.net.load_state_dict(self.checkpoint["state"])
        self.down_sample_rate = self.down_sample_rate

        xvert, xface = load_off(self.mesh_path, to_torch=True)
        self.num_verts = int(xvert.shape[0])

        self.memory_bank = construct_class_by_name(
            **self.memory_bank_params,
            output_size=self.num_verts,
            num_pos=self.num_verts,
            num_noise=0
        ).to(self.device)

        with torch.no_grad():
            self.memory_bank.memory.copy_(
                self.checkpoint["memory"][0 : self.memory_bank.memory.shape[0]]
            )
        memory = (
            self.checkpoint["memory"][0 : self.memory_bank.memory.shape[0]]
            .detach()
            .cpu()
            .numpy()
        )
        clutter = (
            self.checkpoint["memory"][self.memory_bank.memory.shape[0] :]
            .detach()
            .cpu()
            .numpy()
        )
        self.feature_bank = torch.from_numpy(memory)
        self.clutter_bank = torch.from_numpy(clutter).to(self.device)
        self.clutter_bank = normalize_features(
            torch.mean(self.clutter_bank, dim=0)
        ).unsqueeze(0)
        self.kp_features = self.checkpoint["memory"][
            0 : self.memory_bank.memory.shape[0]
        ].to(self.device)

        image_h, image_w = IMAGE_SIZES[self.cate]
        render_image_size = max(image_h, image_w) // self.down_sample_rate
        map_shape = (image_h // self.down_sample_rate, image_w // self.down_sample_rate)

        cameras = construct_class_by_name(**self.cameras_params, device=self.device)
        raster_settings = construct_class_by_name(
            **self.raster_settings_params, image_size=render_image_size
        )
        rasterizer = construct_class_by_name(
            **self.rasterizer_params, cameras=cameras, raster_settings=raster_settings
        )
        self.inter_module = MeshInterpolateModule(
            xvert,
            xface,
            self.feature_bank,
            rasterizer,
            post_process=center_crop_fun(map_shape, (render_image_size,) * 2),
        ).to(self.device)

        (
            azimuth_samples,
            elevation_samples,
            theta_samples,
            distance_samples,
            px_samples,
            py_samples,
        ) = get_param_samples(self.cfg)

        self.poses, self.kp_coords, self.kp_vis = pre_compute_kp_coords(
            self.mesh_path,
            azimuth_samples=azimuth_samples,
            elevation_samples=elevation_samples,
            theta_samples=theta_samples,
            distance_samples=distance_samples,
        )

    def evaluate(self, sample):
        sample = self.transforms(sample)
        img = sample["img"].to(self.device)
        assert len(img) == 1, "The batch size during validation should be 1"

        with torch.no_grad():
            feature_map = self.net.module.forward_test(img)
        pred = solve_pose(
            self.cfg,
            feature_map,
            self.inter_module,
            self.kp_features,
            self.clutter_bank,
            self.poses,
            self.kp_coords,
            self.kp_vis,
            device=self.device,
        )

        if "azimuth" in sample and "elevation" in sample and "theta" in sample:
            pred["pose_error"] = pose_error(sample, pred["final"][0])

        return pred
