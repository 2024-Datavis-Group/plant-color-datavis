# -*- coding: utf-8 -*-
"""“plantimage_to_bookmark.ipynb”的副本

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1-MZY00Y-WbdyFzmHU71h-NlAgIg89et2

——载入谷歌硬盘
"""

from google.colab import drive
drive.mount('/content/drive')

"""——导入包"""

!pip install torch
!pip install git+https://github.com/PyTorchLightning/pytorch-lightning
!pip install segmentation_models_pytorch
!pip install xgboost==1.5.2


import pytorch_lightning as pl
import torch
import segmentation_models_pytorch as smp
import numpy as np
import cv2
from segmentation_models_pytorch.encoders import get_preprocessing_fn
import matplotlib.pyplot as plt
from typing import Dict, List
import pandas as pd
import os

from PIL import Image

!pip install google-cloud-vision

import matplotlib.pyplot as plt
from os import listdir
from matplotlib.colors import rgb_to_hsv, hsv_to_rgb

!pip install colour-science

import colour

from scipy.spatial import Voronoi, voronoi_plot_2d

"""——声明模型"""

class VegAnnModel(pl.LightningModule):
    def __init__(self, arch: str, encoder_name: str, in_channels: int, out_classes: int, **kwargs):
        super().__init__()
        self.model = smp.create_model(
            arch,
            encoder_name=encoder_name,
            in_channels=in_channels,
            classes=out_classes,
            **kwargs,
        )

        # preprocessing parameteres for image
        params = smp.encoders.get_preprocessing_params(encoder_name)
        self.register_buffer("std", torch.tensor(params["std"]).view(1, 3, 1, 1))
        self.register_buffer("mean", torch.tensor(params["mean"]).view(1, 3, 1, 1))

        # for image segmentation dice loss could be the best first choice
        self.loss_fn = smp.losses.DiceLoss(smp.losses.BINARY_MODE, from_logits=True)
        self.train_outputs, self.val_outputs, self.test_outputs = [], [], []

    def forward(self, image: torch.Tensor):
        # normalize image here #todo
        image = (image - self.mean) / self.std
        mask = self.model(image)
        return mask

    def shared_step(self, batch: Dict, stage: str):
        image = batch["image"]

        # Shape of the image should be (batch_size, num_channels, height, width)
        # if you work with grayscale images, expand channels dim to have [batch_size, 1, height, width]
        assert image.ndim == 4

        # Check that image dimensions are divisible by 32,
        # encoder and decoder connected by `skip connections` and usually encoder have 5 stages of
        # downsampling by factor 2 (2 ^ 5 = 32); e.g. if we have image with shape 65x65 we will have
        # following shapes of features in encoder and decoder: 84, 42, 21, 10, 5 -> 5, 10, 20, 40, 80
        # and we will get an error trying to concat these features
        h, w = image.shape[2:]
        assert h % 32 == 0 and w % 32 == 0

        mask = batch["mask"]

        # Shape of the mask should be [batch_size, num_classes, height, width]
        # for binary segmentation num_classes = 1
        assert mask.ndim == 4

        # Check that mask values in between 0 and 1, NOT 0 and 255 for binary segmentation
        assert mask.max() <= 1.0 and mask.min() >= 0

        logits_mask = self.forward(image)

        # Predicted mask contains logits, and loss_fn param `from_logits` is set to True
        loss = self.loss_fn(logits_mask, mask)

        # Lets compute metrics for some threshold
        # first convert mask values to probabilities, then
        # apply thresholding
        prob_mask = logits_mask.sigmoid()
        pred_mask = (prob_mask > 0.5).float()

        # We will compute IoU metric by two ways
        #   1. dataset-wise
        #   2. image-wise
        # but for now we just compute true positive, false positive, false negative and
        # true negative 'pixels' for each image and class
        # these values will be aggregated in the end of an epoch
        tp, fp, fn, tn = smp.metrics.get_stats(pred_mask.long(), mask.long(), mode="binary")

        return {
            "loss": loss,
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": tn,
        }

    def shared_epoch_end(self, outputs: List[Dict], stage: str):
        # aggregate step metics
        tp = torch.cat([x["tp"] for x in outputs])
        fp = torch.cat([x["fp"] for x in outputs])
        fn = torch.cat([x["fn"] for x in outputs])
        tn = torch.cat([x["tn"] for x in outputs])

        # per image IoU means that we first calculate IoU score for each image
        # and then compute mean over these scores
        per_image_iou = smp.metrics.iou_score(tp, fp, fn, tn, reduction="micro-imagewise")
        per_image_f1 = smp.metrics.f1_score(tp, fp, fn, tn, reduction="micro-imagewise")
        per_image_acc = smp.metrics.accuracy(tp, fp, fn, tn, reduction="micro-imagewise")
        # dataset IoU means that we aggregate intersection and union over whole dataset
        # and then compute IoU score. The difference between dataset_iou and per_image_iou scores
        # in this particular case will not be much, however for dataset
        # with "empty" images (images without target class) a large gap could be observed.
        # Empty images influence a lot on per_image_iou and much less on dataset_iou.
        dataset_iou = smp.metrics.iou_score(tp, fp, fn, tn, reduction="micro")
        dataset_f1 = smp.metrics.f1_score(tp, fp, fn, tn, reduction="micro")
        dataset_acc = smp.metrics.accuracy(tp, fp, fn, tn, reduction="micro")

        metrics = {
            f"{stage}_per_image_iou": per_image_iou,
            f"{stage}_dataset_iou": dataset_iou,
            f"{stage}_per_image_f1": per_image_f1,
            f"{stage}_dataset_f1": dataset_f1,
            f"{stage}_per_image_acc": per_image_acc,
            f"{stage}_dataset_acc": dataset_acc,
        }

        self.log_dict(metrics, prog_bar=True, sync_dist=True, rank_zero_only=True)

    def training_step(self, batch: Dict, batch_idx: int):
        step_outputs = self.shared_step(batch, "train")
        self.train_outputs.append(step_outputs)
        return step_outputs

    def on_train_epoch_end(self):
        self.shared_epoch_end(self.train_outputs, "train")
        self.train_outputs = []

    def validation_step(self, batch: Dict, batch_idx: int):
        step_outputs = self.shared_step(batch, "valid")
        self.val_outputs.append(step_outputs)
        return step_outputs

    def on_validation_epoch_end(self, *args, **kwargs):
        self.shared_epoch_end(self.val_outputs, "valid")
        self.val_outputs = []

    def test_step(self, batch: Dict, batch_idx: int):
        step_outputs = self.shared_step(batch, "test")
        self.test_outputs.append(step_outputs)
        return step_outputs

    def on_test_epoch_end(self):
        self.shared_epoch_end(self.test_outputs, "test")
        self.test_outputs = []

    def configure_optimizers(self):
        return torch.optim.Adam(self.parameters(), lr=0.0001)


def colorTransform_VegGround(im,X_true,alpha_vert,alpha_g):
    alpha = alpha_vert
    color = [0,0,0]
    # color = [x / 255 for x in color]
    image=np.copy(im)
    for c in range(3):
        image[:, :, c] =np.where(X_true == 0,image[:, :, c] *(1 - alpha) + alpha * color[c] ,image[:, :, c])
    alpha = alpha_g
    color = [34,139,34]
#    color = [x / 255 for x in color]
    for c in range(3):
        image[:, :, c] =np.where(X_true == 1,image[:, :, c] *(1 - alpha) + alpha * color[c] ,image[:, :, c])
    return image

"""——载入权重"""

!gdown https://drive.google.com/uc?id=1azagsinfW4btSGaTi0XJKsRnFR85Gtaw
ckt_path = "/content/VegAnn.ckpt"


checkpoint = torch.load(ckt_path, map_location=torch.device('cpu'))
model = VegAnnModel("Unet","resnet34",in_channels = 3, out_classes=1 )
model.load_state_dict(checkpoint["state_dict"])
preprocess_fn = smp.encoders.get_preprocessing_fn("resnet34", pretrained= "imagenet")
model.eval()

"""————基本信息————"""

export_path = "/content/drive/MyDrive/XinHuaCummunity/"  # 将此路径替换为你的导出目录
base_name = "5"  # 将此替换为你的原始图像的基本名称，不包含扩展名

"""——切成4份"""

def split_and_save_image(image_path, export_path):
    # 加载图片
    image = Image.open(image_path)
    # 计算分割点
    width, height = image.size
    center = (width / 2, height / 2)

    # 定义四个区域的边界：左上、左下、右上、右下
    left_up = (0, 0, center[0], center[1])
    left_down = (0, center[1], center[0], height)
    right_up = (center[0], 0, width, center[1])
    right_down = (center[0], center[1], width, height)

    # 分割图片
    images = {
        "_1": image.crop(left_up),
        "_2": image.crop(left_down),
        "_3": image.crop(right_up),
        "_4": image.crop(right_down),
    }

    # 获取原文件名
    base_name = os.path.splitext(os.path.basename(image_path))[0]

    # 分别保存四个部分
    for suffix, img in images.items():
        img.save(os.path.join(export_path, f"{base_name}{suffix}.jpg"))


split_and_save_image(export_path+base_name+'.jpg', export_path)

"""——实例分割过程"""

import glob

def resize_image(image_path, new_width, new_height):
    img = cv2.imread(image_path)
    resized_img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
    return resized_img

empt = pd.DataFrame(columns=['B', 'G', 'R'])
empt.to_excel('mask_color_data.xlsx', index=False)

i=1

##for filename in os.listdir('/content/drive/MyDrive/XinHuaCummunity/'+'2_*'): ##使用批处理的话前面要取消缩进               ###############################原始图片文件夹路径
  ##image = resize_image('/content/drive/MyDrive/XinHuaCummunity/'+'/'+filename, 384, 512)    #################################原始图片文件夹路径
  ##image = resize_image('/content/drive/MyDrive/XinHuaCummunity/2.jpg', 384, 512)



# 构建用于glob的模式，以匹配特定的文件名模式
pattern = os.path.join(export_path, f"{base_name}_?.jpg")  # ? 代表单个字符，这里假设是1到4

# 使用glob遍历匹配的文件
for image_path in glob.glob(pattern):
  image = resize_image(image_path, 384, 512)
  im = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)

  preprocess_input = get_preprocessing_fn('resnet34', pretrained='imagenet')
  image = preprocess_input(im)
  image = image.astype('float32')


  inputs = torch.tensor(image) # , dtype=float
  # print(inputs.size)
  inputs = inputs.permute(2,0,1)
  inputs = inputs[None,:,:,:]
  # print(inputs.shape)
  logits = model(inputs)
  pr_mask = logits.sigmoid()

  pred = (pr_mask > 0.5).numpy().astype(np.uint8)

  im1_pred = colorTransform_VegGround(im,pred,0.8,0.2)
  im2_pred = colorTransform_VegGround(im,pred,1,0)


  # 获取掩膜区域的平均BGR颜色
  mask_color = np.mean(im2_pred, axis=(0, 1))
  # 将 NumPy 数组转换为 Pandas Series
  mask_color_series = pd.Series(mask_color)

  # 读取现有的 DataFrame
  existing_data = pd.read_excel('mask_color_data.xlsx')

  # 将 mask_color_series 添加到 DataFrame
  combined_data = existing_data.append(mask_color_series, ignore_index=True)

  # 将合并后的数据保存到 Excel 文件
  combined_data.to_excel('mask_color_data.xlsx', index=False)

  fig, (ax1, ax2) = plt.subplots(1, 2)
  ax1.imshow(im)
  ax1.set_title("Input Image")

  ax2.imshow(im2_pred)
  ax2.set_title("Prediction")
  plt.show()

 ## plt.imsave('/content/drive/MyDrive/XinHuaCummunity/'+'/'+filename+'.png',im2_pred)    ################################实例分割文件夹路径
  plt.imsave(export_path+base_name+'_{}'.format(i)+'.png',im2_pred)
  i+=1

"""——Google vision ai色彩提取"""

from google.cloud import vision

# 设置你的 Google Cloud 认证信息
import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/content/drive/MyDrive/bustling-wharf-359411-b33e8c55e506.json"

def detect_image_properties(image_path,threshold):
    client = vision.ImageAnnotatorClient()

    # 读取图像文件
    with open(image_path, 'rb') as image_file:
        content = image_file.read()

    image = vision.Image(content=content)

    # 发送图像请求以检测属性
    response = client.image_properties(image=image)

    # 解析检测结果
    properties = response.image_properties_annotation

    # 获取主色调
    main_colors = properties.dominant_colors.colors

    # 提取 pixel_fraction 值大于threshold的颜色信息，并将其转换为多维矩阵
    color_matrix = [
        [color.color.red,color.color.green,color.color.blue,color.pixel_fraction]#

        for color in main_colors
        if color.pixel_fraction >= threshold
    ]

    color_matrix = np.array(color_matrix)
    return color_matrix


    # function to visualize array of colors
def palette(colors):
    # input: array of RGB colors
    num_colors = len(colors)
    pal = np.zeros((100, 100 * num_colors, 3))
    for c in range(len(colors)):
        pal[:,c*100:c*100+100, 0] = colors[c][0]
        pal[:,c*100:c*100+100, 1] = colors[c][1]
        pal[:,c*100:c*100+100, 2] = colors[c][2]
    plt.xticks([])
    plt.yticks([])
    plt.axis('off')  # 隐藏坐标轴
    plt.imshow(pal/255)
    return

    import pandas as pd

def save_colors_to_csv(color_matrix, output_csv_path):
    # 将颜色数据转换为Pandas DataFrame
    df = pd.DataFrame(list(color_matrix), columns=['r', 'g', 'b', 'radio'])
    # 保存到CSV文件
    df.to_csv(output_csv_path, index=False)
    print(df)

"""——提取主导色批处理"""

# 构建用于glob的模式，以匹配特定的文件名模式
pattern = os.path.join(export_path, f"{base_name}_?.png")  ####效果不好就用实例分割前.jpg#####
# 使用glob遍历匹配的文件
for image_path in glob.glob(pattern):
    colors = detect_image_properties(image_path, 0.005)   #实例分割后图片路径
    save_colors_to_csv(colors, image_path+'.csv')           #色彩提取表格路径
    palette(colors)

"""********提取主导色单张(不用运行这一块"""

colors = detect_image_properties('/content/drive/MyDrive/XinHuaCummunity/2.jpg', 0.005)   ##############实例分割后图片路径
save_colors_to_csv(colors, '/content/drive/MyDrive/XinHuaCummunity/2.csv')           ##############色彩提取表格路径
palette(colors)

"""——处理表格（删除黑色）"""

def process_csv_single_param(file_path):
    """
    Processes the CSV file to filter out rows where the sum of r, g, b is greater than 150,
    adds a 'Ratio' column to calculate each row's radio in relation to the sum of the 'radio' column,
    and adds an 'id' column filled with the filename (without the extension) derived from the file path.

    Parameter:
    - file_path: The path to the CSV file.

    Returns:
    - A pandas DataFrame after applying the above operations.
    """
    # Extract the filename without the extension from the file path
    filename_without_extension = file_path.split('/')[-1].split('.')[0]

    # Load the CSV file
    df = pd.read_csv(file_path)

    # Filter rows where the sum of r, g, b is less than or equal to 150
    filtered_df = df[df[['r', 'g', 'b']].sum(axis=1) >= 50]

    # Calculate the total 'radio' for the ratio calculation
    total_radio = filtered_df['radio'].sum()

    # Add 'Ratio' column
    filtered_df['Ratio'] = filtered_df['radio'] / total_radio

    # Add 'id' column with the filename without extension
    filtered_df['id'] = filename_without_extension

    return filtered_df

def process_csv(input_filename,output_folder_path):

    processed_df = process_csv_single_param(input_filename)

    processed_files = {}

    processed_files[input_filename.split('/')[-1].split('.')[0]] = processed_df
    print(processed_files.keys())
    print(processed_files)
    # Ensure the output folder exists, if not, create it
    os.makedirs(output_folder_path, exist_ok=True)

    #file_base_name = filename.split('.')[0]
    for filename, df in processed_files.items():
        output_file_path = os.path.join(output_folder_path, f"{filename}_processed.csv")
        processed_df.to_csv(output_file_path, index=False)
    return f"Processed files saved to {output_folder_path}"

pattern = os.path.join(export_path, f"{base_name}_?.png.csv")  # ? 代表单个字符，这里假设是1到4
for csv_path in glob.glob(pattern):
  result_message = process_csv(csv_path, export_path)   #######色彩提取表格名；处理后表格路径
  #print(result_message)

"""——拼接表格"""

def concatenate_csv(output_filename):
    # 初始化一个空的DataFrame来存储结果
    combined_df = pd.DataFrame()

    # 遍历文件夹中的所有文件
    pattern = os.path.join(export_path, f"{base_name}_?_processed.csv")  # ? 代表单个字符，这里假设是1到4
    for csv_prossed_path in glob.glob(pattern):
            # 读取CSV文件
            df = pd.read_csv(csv_prossed_path)
            # 将读取的数据拼接到结果DataFrame中
            combined_df = pd.concat([combined_df, df], ignore_index=True)

    # 将结果保存到新的CSV文件中
    combined_df.to_csv(output_filename, index=False)
    print(f'Combined CSV saved as {output_filename}')

# 使用示例
output_filename = export_path+'combined_'+base_name+'.csv' # 输出文件的名称

# 调用函数
concatenate_csv(output_filename)

"""——转换HSV"""

def rgb_to_hsv_normalized(r, g, b):
    # 将RGB值从0-255范围转换到0-1范围
    r_normalized, g_normalized, b_normalized = r / 255.0, g / 255.0, b / 255.0
    # 使用colour库转换RGB到HSV
    hsv = colour.RGB_to_HSV((r_normalized, g_normalized, b_normalized))
    return hsv

def process_csv_with_colour(input_filename, output_filename):
    # 读取CSV文件
    df = pd.read_csv(input_filename)

    # 应用转换函数并分别存储结果
    hsv_values = df.apply(lambda row: rgb_to_hsv_normalized(row['r'], row['g'], row['b']), axis=1)
    df['Hue'], df['Saturation'], df['Value'] = zip(*hsv_values)

    # 保存到新的CSV文件
    df.to_csv(output_filename, index=False)
    print(f'Processed file saved as {output_filename}')

# 示例使用
input_filename = export_path+'combined_'+base_name+'.csv'       ######## combined表格文件名
output_filename = export_path+base_name+'_processed_HSV.csv'     ######## combined HSV表格文件名

# 调用函数
process_csv_with_colour(input_filename, output_filename)

"""——画泰森多边形填色&色彩比例圆形填色"""

#######————泰森多边形版————

# 读取颜色数据
def read_color_data(csv_file_path):
    color_data = pd.read_csv(csv_file_path)
    return color_data[['r', 'g', 'b']].values, color_data['Ratio'].values

# 生成并填色泰森多边形，确保角落区域也着色
def generate_colored_voronoi(csv_file_path, output_image_path, width=1024, height=768):
    colors, ratios = read_color_data(csv_file_path)
    color_probabilities = ratios / np.sum(ratios)

    # 生成种子点
    seed_points = np.random.rand(len(colors) * 30, 2) * [width, height]

    # 在边界外添加一圈点以覆盖整个边界，确保包括角落在内的每个区域都被涂色
    border_padding = 100  # 边界外扩展的距离
    boundary_points = np.array([
        [x, y]
        for x in np.linspace(-border_padding, width + border_padding, num=4)
        for y in np.linspace(-border_padding, height + border_padding, num=4)
    ])
    points = np.vstack([seed_points, boundary_points])
    vor = Voronoi(points)

    # 绘制并填色
    fig, ax = plt.subplots(figsize=(width / 100, height / 100), dpi=100)
    plt.axis('off')
    for region in vor.regions:
        if not -1 in region and len(region) > 0:
            polygon = [vor.vertices[i] for i in region]
            color_idx = np.random.choice(len(colors), p=color_probabilities)
            color = colors[color_idx] / 255
            plt.fill(*zip(*polygon), color=color)

    plt.xlim(0, width)
    plt.ylim(0, height)
    plt.gca().invert_yaxis()
    plt.axis('off')
    plt.savefig(output_image_path, bbox_inches='tight', pad_inches=0)


    plt.show()

    plt.close()



###########——圆形版本尝试——


def generate_floral_pattern(csv_file_path, output_image_path):
    # 读取颜色和比例
    color_data = pd.read_csv(csv_file_path)
    colors = color_data[['r', 'g', 'b']].values
    ratios = color_data['Ratio'].values

    # 计算圆的半径，假设最大比例的圆半径为80，其他按比例计算
    radii = np.sqrt(ratios / np.pi) * 35  # 减小基础半径使得排列更紧凑

    # 排序，以便将最大的圆置于中心
    indices = np.argsort(radii)  #-
    sorted_radii = radii[indices]
    sorted_colors = colors[indices] / 255.0  # 归一化颜色

    # 绘图
    fig, ax = plt.subplots()
    center = np.array([0, 0])  # 中心圆的位置
    ax.add_artist(plt.Circle(center, sorted_radii[0], color=sorted_colors[0]))

    # 初始化第一圈的半径和位置
    current_radius = sorted_radii[0] + sorted_radii[1] + 8  # 减少初始的空隙使得排列更紧凑
    theta = np.pi / 4
    for i in range(1, len(sorted_radii)):
        circle_radius = sorted_radii[i]
        position = center + np.array([np.cos(theta), np.sin(theta)]) * current_radius
        ax.add_artist(plt.Circle(position, circle_radius, color=sorted_colors[i]))

        # 更新角度和半径以放置下一个圆
        theta += np.pi / 5  # 示例中简化为每隔45度放置一个圆
        current_radius += circle_radius * 0.8  # 减小圆之间的空隙

        # 调整角度和半径，以便圆形在空间中合理分布
        if theta >= 2 * np.pi:
            ##theta = 0
            current_radius += 1  # 在每一层之间留有空隙

    # 设置图形属性
    ax.set_aspect('equal')
    ax.set_xlim(-250, 250)      ###ax.set_xlim(-250, 250)
    ax.set_ylim(-250, 250)      ###ax.set_ylim(-250, 250)
    plt.axis('off')

    # 保存到文件
    plt.savefig(output_image_path, bbox_inches='tight')


    plt.show()

    plt.close()







# 画！
csv_file_path = export_path+base_name+'_processed_HSV.csv'        ######## combined HSV表格文件名
output_image_path1 = export_path+'vrinoi_'+base_name+'.png'                   ######## vrinoi文件名
output_image_path2 = export_path+'floral_pattern_'+base_name+'.png'           ######## floral_pattern文件名
generate_colored_voronoi(csv_file_path, output_image_path1)
generate_floral_pattern(csv_file_path, output_image_path2)

# 画！
csv_file_path = export_path+base_name+'_processed_HSV.csv'        ######## combined HSV表格文件名
output_image_path1 = export_path+'vrinoi_'+base_name+'.png'                   ######## vrinoi文件名
output_image_path2 = export_path+'floral_pattern_'+base_name+'.png'           ######## floral_pattern文件名
generate_colored_voronoi(csv_file_path, output_image_path1)
generate_floral_pattern(csv_file_path, output_image_path2)