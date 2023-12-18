from easydict import EasyDict as edict
import json

config = edict()
config.TRAIN = edict()
config.TRAIN.batch_size = 8 # [16] use 8 if your GPU memory is small
config.TRAIN.lr_init = 1e-4
config.TRAIN.beta1 = 0.9

## initialize G
config.TRAIN.n_epoch_init = 100 #100
    # config.TRAIN.lr_decay_init = 0.1
    # config.TRAIN.decay_every_init = int(config.TRAIN.n_epoch_init / 2)

## adversarial learning (SRGAN)
config.TRAIN.n_epoch = 8000 #8000
config.TRAIN.lr_decay = 0.1
config.TRAIN.decay_every = int(config.TRAIN.n_epoch / 2)

## train set location
config.TRAIN.hr_img_path = '/home/student515/Documents/thesis/Dataset/SRGAN_train_aug'
#config.TRAIN.lr_img_path = '/home/student515/Documents/thesis/Dataset/SRGAN_train_aug_LR'

config.VALID = edict()
## validation set location
config.VALID.hr_img_path = '/home/student515/Documents/thesis/Dataset/SRGAN_val'
#config.VALID.lr_img_path = '/home/student515/Documents/thesis/Dataset/SRGAN_val_LR'

def log_config(filename, cfg):
    with open(filename, 'w') as f:
        f.write("================================================\n")
        f.write(json.dumps(cfg, indent=4))
        f.write("\n================================================\n")
