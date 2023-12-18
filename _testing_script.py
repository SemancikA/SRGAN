import cv2
import numpy as np

# Example: Load an image and resize it
image_path = '/home/student515/Documents/thesis/Dataset/SRGAN_train_aug/0_0_AlSi10Mg-000000_segment_0.tif'
image = cv2.imread(image_path)

if image is not None:
    resized_image = cv2.resize(image, (400, 400))
    print("Image loaded correctly")
else:
    print("Image not loaded correctly")

