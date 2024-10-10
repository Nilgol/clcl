import glob
import os
import pickle
import numpy as np
import cv2
import albumentations as A
from albumentations.pytorch import ToTensorV2
import torch
from torch.utils.data import Dataset
from torchvision.transforms import v2
import matplotlib.pyplot as plt
from .a2d2_utils import load_config, undistort_image, random_crop


class A2D2Dataset(Dataset):
    def __init__(
        self,
        root_path="/homes/math/golombiewski/workspace/data/A2D2",
        config_path="/homes/math/golombiewski/workspace/data/A2D2_general/cams_lidars.json",
        missing_keys_file="/homes/math/golombiewski/workspace/clcl/data/missing_keys_files.pkl",
        missing_point_clouds_file="/homes/math/golombiewski/workspace/clcl/data/empty_point_clouds.pkl",
        crop_size=(896, 896),
        val_ratio=0.2,
        split="train",
        augment=False,
    ):
        self.root_path = root_path
        self.crop_size = crop_size
        self.config = load_config(config_path)
        self.missing_keys_files = self._load_missing_keys_file(missing_keys_file)
        self.missing_point_clouds = self._load_missing_point_clouds_file(
            missing_point_clouds_file
        )
        self.augment = augment

        if self.augment:
            augmentations = A.Compose([
                A.OneOf([
                    A.ColorJitter(brightness=0.3, contrast=0.3, saturation=0.3, hue=0.1, p=0.7),
                    A.RandomBrightnessContrast(
                        brightness_limit=(-0.35, -0.2),
                        contrast_limit=(-0.35, -0.2),
                        p=0.2),
                    A.ToGray(p=0.1),
                ], p=1.0),
                
                A.OneOf([
                    A.GaussianBlur(blur_limit=(3, 9), sigma_limit=0, p=1.0),
                    A.GlassBlur(sigma=0.7, max_delta=1, iterations=2, mode="fast", p=1.0),
                    A.GaussNoise(var_limit=(10.0, 500.0), mean=0, per_channel=True, p=1.0),
                ], p=0.5),

            ], p=0.9)
        else:
            augmentations = None

        default_transforms = A.Compose([
            A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
            ToTensorV2(),
        ])

        if augmentations is not None:
            self.image_transform = A.Compose([augmentations, default_transforms])
        else:
            self.image_transform = default_transforms


        if val_ratio < 0 or val_ratio > 0.5:
            val_ratio = max(0, min(val_ratio, 0.5))
            print(
                f"Warning: val_ratio should be between 0 and 0.5. Setting it to {val_ratio}."
            )
        self.val_ratio = val_ratio

        self.train_data_pairs, self.val_data_pairs = self._create_data_pairs()

        if split == "train":
            self.data_pairs = self.train_data_pairs
        elif split == "val":
            self.data_pairs = self.val_data_pairs
        else:
            raise ValueError(f"Invalid split value: {split}. Use 'train' or 'val'.")

    def _load_missing_keys_file(self, missing_keys_file):
        with open(missing_keys_file, "rb") as f:
            missing_keys_files = pickle.load(f)
        return missing_keys_files

    def _load_missing_point_clouds_file(self, missing_point_clouds_file):
        with open(missing_point_clouds_file, "rb") as f:
            missing_point_clouds = pickle.load(f)
        return missing_point_clouds

    def _create_data_pairs(self):
        lidar_paths = sorted(
            glob.glob(os.path.join(self.root_path, "*/lidar/cam_front_center/*.npz"))
        )
        scene_dict = {}

        for lidar_path in lidar_paths:
            if (
                lidar_path in self.missing_keys_files
                or lidar_path in self.missing_point_clouds
            ):
                continue
            seq_name = lidar_path.split("/")[-4]
            if seq_name not in scene_dict:
                scene_dict[seq_name] = []
            scene_dict[seq_name].append(lidar_path)

        train_data_pairs = []
        val_data_pairs = []

        for seq_name, lidar_paths in scene_dict.items():
            num_samples = len(lidar_paths)
            num_val_samples = int(num_samples * self.val_ratio)
            num_train_samples = num_samples - num_val_samples

            train_lidar_paths = lidar_paths[:num_train_samples]
            val_lidar_paths = lidar_paths[num_train_samples:]

            for lidar_path in train_lidar_paths:
                image_file_name = (
                    lidar_path.split("/")[-1]
                    .replace("lidar", "camera")
                    .replace(".npz", ".png")
                )
                image_path = os.path.join(
                    self.root_path, seq_name, "camera/cam_front_center", image_file_name
                )
                train_data_pairs.append((lidar_path, image_path))

            for lidar_path in val_lidar_paths:
                image_file_name = (
                    lidar_path.split("/")[-1]
                    .replace("lidar", "camera")
                    .replace(".npz", ".png")
                )
                image_path = os.path.join(
                    self.root_path, seq_name, "camera/cam_front_center", image_file_name
                )
                val_data_pairs.append((lidar_path, image_path))

        return train_data_pairs, val_data_pairs

    def __len__(self):
        return len(self.data_pairs)

    def _getitem_unsafe(self, idx):
        lidar_path, image_path = self.data_pairs[idx]
        lidar_data = np.load(lidar_path)

        point_cloud = np.hstack(
            (
                lidar_data["points"],
                lidar_data["reflectance"].reshape(-1, 1),
                lidar_data["row"].reshape(-1, 1),
                lidar_data["col"].reshape(-1, 1),
            )
        )

        image = cv2.imread(image_path)
        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        undistorted_image = undistort_image(image, "front_center", self.config)

        if self.crop_size:
            undistorted_image, point_cloud = random_crop(
                undistorted_image, point_cloud, self.crop_size
            )

        undistorted_image = cv2.resize(undistorted_image, (224, 224))

        if self.image_transform:
            transformed_image = self.image_transform(image=undistorted_image)['image']

        points_tensor = torch.tensor(point_cloud[:, :4], dtype=torch.float32)

        return transformed_image, points_tensor

    def __getitem__(self, idx):
        """Random crop might discard ALL points from the point cloud.
        If the point cloud is empty, try the next sample."""
        max_attempts = 100
        attempt = 0

        while attempt < max_attempts:
            image, point_cloud = self._getitem_unsafe(idx)
            if point_cloud.size(0) > 0:  # Point cloud non-empty?
                return image, point_cloud
            else:
                attempt += 1
                idx = (idx + 1) % len(self.data_pairs)  # Try next index

        raise RuntimeError(
            f"Could not find a valid sample after {max_attempts} attempts starting from index {idx}"
        )


if __name__ == "__main__":
    # Basic functionality tests
    crop = 896
    train_set = A2D2Dataset(crop_size=(crop, crop), val_ratio=0.1, split="train", augment=False)
    val_set = A2D2Dataset(crop_size=(crop, crop), val_ratio=0.1, split="val", augment=False)
    num_samples = 200
    output_dir = "/homes/math/golombiewski/workspace/fast/crop_resized_images"
    os.makedirs(output_dir, exist_ok=True)

    def denormalize_image(tensor, mean, std):
        mean = torch.tensor(mean).view(1, 3, 1, 1)
        std = torch.tensor(std).view(1, 3, 1, 1)
        tensor = tensor * std + mean
        return tensor
    
    mean = [0.485, 0.456, 0.406]
    std = [0.229, 0.224, 0.225]

    import random
    indices = list(range(len(train_set)))
    random.shuffle(indices)

    # Iterate over the dataset and save the images
    for i in indices[:num_samples]:
        image, _ = train_set[i]
        
        # Denormalize the image
        image = denormalize_image(image.unsqueeze(0), mean, std).squeeze(0)
        
        # Convert tensor to a NumPy array for saving
        image_np = image.permute(1, 2, 0).numpy()  # CHW to HWC
        image_np = (image_np * 255).astype(np.uint8)
        
        # Save the image
        cv2.imwrite(os.path.join(output_dir, f"image_{i}.png"), cv2.cvtColor(image_np, cv2.COLOR_RGB2BGR))
        print(f"Saved image_{i}.png")

    print(f"{num_samples} cropped and resized images saved in '{output_dir}' directory.")


    # Check dataset length
    # print(f"Total train pairs: {len(train_set)}")
    # print(f"Total val pairs: {len(val_set)}")

    # ratios = collect_point_retention_ratios(dataset, num_samples)

    # Check random data pairs
    # for i in range(num_samples):
    #     idx = np.random.randint(0, len(dataset))

    #     lidar_path, image_path = dataset.data_pairs[idx]
    #     image_tensor, points_tensor = dataset[idx]
    #     print(f"Pair {i+1}:")
    # print(f"  Image size: {cv2.imread(image_path).shape}")
    # print(f"  Number of points: {np.load(lidar_path)['points'].shape[0]}")
    # print(f"  Image tensor shape: {image_tensor.shape}")
    # print(f"  Points tensor shape: {points_tensor.shape}")
    # print('Number of points after cropping:', points_tensor.shape[0])
    # print('Ratio of points after cropping:', points_tensor.shape[0] / np.load(lidar_path)['points'].shape[0])

    # Analyze the ratios
    # print(f"Mean points retained ratio: {ratios.mean()}")
    # print(f"Median points retained ratio: {np.median(ratios)}")
    # print(f"Min points retained ratio: {ratios.min()}")
    # print(f"Max points retained ratio: {ratios.max()}")
    # print(f"Standard deviation: {ratios.std()}")