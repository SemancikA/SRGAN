import os
os.environ['TL_BACKEND'] = 'tensorflow' # Just modify this line, easily switch to any framework! PyTorch will coming soon!
# os.environ['TL_BACKEND'] = 'mindspore'
# os.environ['TL_BACKEND'] = 'paddle'
#os.environ['TL_BACKEND'] = 'torch'
import time
import numpy as np
import tensorlayerx as tlx
from tensorlayerx.dataflow import Dataset, DataLoader
from srgan import SRGAN_g, SRGAN_d
from config import config
from tensorlayerx.vision.transforms import Compose, RandomCrop, Normalize, RandomFlipHorizontal, Resize, HWC2CHW
import vgg
from tensorlayerx.model import TrainOneStep
from tensorlayerx.nn import Module
import cv2

import tensorflow as tf
import statistics as st
tlx.set_device('GPU')

###====================== HYPER-PARAMETERS ===========================###
batch_size = 4
n_epoch_init = config.TRAIN.n_epoch_init
n_epoch = config.TRAIN.n_epoch
# create folders to save result images and trained models
save_dir = "samples"
tlx.files.exists_or_mkdir(save_dir)
checkpoint_dir = "models"
tlx.files.exists_or_mkdir(checkpoint_dir)

hr_transform = Compose([
    RandomCrop(size=(384, 384)),
    RandomFlipHorizontal(),
])
nor = Compose([Normalize(mean=(127.5), std=(127.5), data_format='HWC'),
              HWC2CHW()])
lr_transform = Resize(size=(96, 96))

train_hr_imgs = tlx.vision.load_images(path=config.TRAIN.hr_img_path, n_threads = 32)

class TrainData(Dataset):

    def __init__(self, hr_trans=hr_transform, lr_trans=lr_transform):
        self.train_hr_imgs = train_hr_imgs
        self.hr_trans = hr_trans
        self.lr_trans = lr_trans

    def __getitem__(self, index):
        img = self.train_hr_imgs[index]
        hr_patch = self.hr_trans(img)
        lr_patch = self.lr_trans(hr_patch)
        return nor(lr_patch), nor(hr_patch)

    def __len__(self):
        return len(self.train_hr_imgs)
    
valid_hr_imgs = tlx.vision.load_images(path=config.VALID.hr_img_path, n_threads = 32)

class ValidData(Dataset):

    def __init__(self, hr_trans=hr_transform, lr_trans=lr_transform):
        self.valid_hr_imgs = valid_hr_imgs
        self.hr_trans = hr_trans
        self.lr_trans = lr_trans

    def __getitem__(self, index):
        img = self.valid_hr_imgs[index]
        hr_valid_patch = self.hr_trans(img)
        lr_valid_patch = self.lr_trans(hr_valid_patch)
        return nor(lr_valid_patch), nor(hr_valid_patch)

    def __len__(self):
        return len(self.valid_hr_imgs)


class WithLoss_init(Module):
    def __init__(self, G_net, loss_fn):
        super(WithLoss_init, self).__init__()
        self.net = G_net
        self.loss_fn = loss_fn

    def forward(self, lr, hr):
        out = self.net(lr)
        loss = self.loss_fn(out, hr)
        return loss


class WithLoss_D(Module):
    def __init__(self, D_net, G_net, loss_fn):
        super(WithLoss_D, self).__init__()
        self.D_net = D_net
        self.G_net = G_net
        self.loss_fn = loss_fn

    def forward(self, lr, hr):
        fake_patchs = self.G_net(lr)
        logits_fake = self.D_net(fake_patchs)
        logits_real = self.D_net(hr)
        d_loss1 = self.loss_fn(logits_real, tlx.ones_like(logits_real))
        d_loss1 = tlx.ops.reduce_mean(d_loss1)
        d_loss2 = self.loss_fn(logits_fake, tlx.zeros_like(logits_fake))
        d_loss2 = tlx.ops.reduce_mean(d_loss2)
        d_loss = d_loss1 + d_loss2
        return d_loss


class WithLoss_G(Module):
    def __init__(self, D_net, G_net, vgg, loss_fn1, loss_fn2):
        super(WithLoss_G, self).__init__()
        self.D_net = D_net
        self.G_net = G_net
        self.vgg = vgg
        self.loss_fn1 = loss_fn1
        self.loss_fn2 = loss_fn2

    def forward(self, lr, hr):
        fake_patchs = self.G_net(lr)
        logits_fake = self.D_net(fake_patchs)
        feature_fake = self.vgg((fake_patchs + 1) / 2.)
        feature_real = self.vgg((hr + 1) / 2.)
        g_gan_loss = 1e-3 * self.loss_fn1(logits_fake, tlx.ones_like(logits_fake))
        g_gan_loss = tlx.ops.reduce_mean(g_gan_loss)
        mse_loss = self.loss_fn2(fake_patchs, hr)
        vgg_loss = 2e-6 * self.loss_fn2(feature_fake, feature_real)
        g_loss = mse_loss + vgg_loss + g_gan_loss
        return g_loss


G = SRGAN_g()
D = SRGAN_d()
VGG = vgg.VGG19(pretrained=True, end_with='pool4', mode='dynamic')
# automatic init layers weights shape with input tensor.
# Calculating and filling 'in_channels' of each layer is a very troublesome thing.
# So, just use 'init_build' with input shape. 'in_channels' of each layer will be automaticlly set.
G.init_build(tlx.nn.Input(shape=(8, 3, 96, 96)))
D.init_build(tlx.nn.Input(shape=(8, 3, 384, 384)))

train_writer = tf.summary.create_file_writer('logs/train/'+time.strftime("%Y_%m_%d-%H_%M_%S"))
init_writer = tf.summary.create_file_writer('logs/train/'+time.strftime("%Y_%m_%d-%H_%M_%S"))

init_loss = []
losses_g = []
losses_d = []
psnr_values=[]
ssim_values=[]

valid_crop = RandomCrop(size=(1536, 1536))
valid_save_crop = tlx.vision.transforms.Crop(0, 0, 1536, 1536)

def train():
    G.set_train()
    D.set_train()
    VGG.set_eval()
    train_ds = TrainData()
    train_ds_img_nums = len(train_ds)
    train_ds = DataLoader(train_ds, batch_size=batch_size, shuffle=True, drop_last=True)

    valid_ds = ValidData()
    valid_ds_img_nums = len(valid_ds)
    valid_ds = DataLoader(train_ds, shuffle=True, drop_last=True)

    lr_v = tlx.optimizers.lr.StepDecay(learning_rate=0.05, step_size=1000, gamma=0.1, last_epoch=-1, verbose=True)
    g_optimizer_init = tlx.optimizers.Momentum(lr_v, 0.9)
    g_optimizer = tlx.optimizers.Momentum(lr_v, 0.9)
    d_optimizer = tlx.optimizers.Momentum(lr_v, 0.9)
    g_weights = G.trainable_weights
    d_weights = D.trainable_weights
    net_with_loss_init = WithLoss_init(G, loss_fn=tlx.losses.mean_squared_error)
    net_with_loss_D = WithLoss_D(D_net=D, G_net=G, loss_fn=tlx.losses.sigmoid_cross_entropy)
    net_with_loss_G = WithLoss_G(D_net=D, G_net=G, vgg=VGG, loss_fn1=tlx.losses.sigmoid_cross_entropy,
                                 loss_fn2=tlx.losses.mean_squared_error)

    trainforinit = TrainOneStep(net_with_loss_init, optimizer=g_optimizer_init, train_weights=g_weights)
    trainforG = TrainOneStep(net_with_loss_G, optimizer=g_optimizer, train_weights=g_weights)
    trainforD = TrainOneStep(net_with_loss_D, optimizer=d_optimizer, train_weights=d_weights)

    # initialize learning (G)
    n_step_epoch = round(train_ds_img_nums // batch_size)
    for epoch in range(n_epoch_init):
        for step, (lr_patch, hr_patch) in enumerate(train_ds):
            step_time = time.time()
            loss = trainforinit(lr_patch, hr_patch)

            init_loss.append(loss)

            print("Epoch: [{}/{}] step: [{}/{}] time: {:.3f}s, mse: {:.3f} ".format(
                epoch, n_epoch_init, step, n_step_epoch, time.time() - step_time, float(loss)))
        
        with init_writer.as_default():
            tf.summary.scalar('mse', float(st.mean(init_loss)), step=epoch)

    # adversarial learning (G, D)
    n_step_epoch = round(train_ds_img_nums // batch_size)
    for epoch in range(n_epoch):
        for step, (lr_patch, hr_patch) in enumerate(train_ds):
            step_time = time.time()
            loss_g = trainforG(lr_patch, hr_patch)
            loss_d = trainforD(lr_patch, hr_patch)

            #create list for loss_g and loss_d
            losses_g.append(loss_g)
            losses_d.append(loss_d)

            print(
                "Epoch: [{}/{}] step: [{}/{}] time: {:.3f}s, g_loss:{:.3f}, d_loss: {:.3f}".format(
                    epoch, n_epoch, step, n_step_epoch, time.time() - step_time, float(loss_g), float(loss_d)))
            
            #gif validate
            if step % 10 == 0:
                #validation 
                G.set_eval()
                valid_hr_img = valid_hr_imgs[0]
                valid_hr_img = valid_save_crop(valid_hr_img)
                valid_lr_img = np.asarray(valid_hr_img)
                hr_size1 = [valid_lr_img.shape[0], valid_lr_img.shape[1]]
                valid_lr_img = cv2.resize(valid_lr_img, dsize=(hr_size1[1] // 4, hr_size1[0] // 4))
                valid_lr_img_tensor = (valid_lr_img / 127.5) - 1  # rescale to ［－1, 1]

                valid_lr_img_tensor = np.asarray(valid_lr_img_tensor, dtype=np.float32)
                valid_lr_img_tensor = np.transpose(valid_lr_img_tensor, axes=[2, 0, 1])
                valid_lr_img_tensor = valid_lr_img_tensor[np.newaxis, :, :, :]
                valid_lr_img_tensor = tlx.ops.convert_to_tensor(valid_lr_img_tensor)
                
                out = tlx.ops.convert_to_numpy(G(valid_lr_img_tensor))
                out = np.asarray((out + 1) * 127.5, dtype=np.uint8)
                out = np.transpose(out[0], axes=[1, 2, 0])
                size = [valid_lr_img.shape[0], valid_lr_img.shape[1]]

                tlx.vision.save_image(out, file_name = 'valid_gen'+str(epoch)+'_'+str(step)+'.tif',path=save_dir)
                tlx.vision.save_image(valid_hr_img, file_name = 'valid_hr'+str(epoch)+'_'+str(step)+'.tif', path=save_dir)
                out_bicu = cv2.resize(valid_lr_img, dsize = [size[1] * 4, size[0] * 4], interpolation = cv2.INTER_CUBIC)
                tlx.vision.save_image(out_bicu, file_name='valid_hr_cubic.tif', path=save_dir)
                G.set_train()
        
            
        with train_writer.as_default():
            tf.summary.scalar('g_loss', float(st.mean(losses_g)), step=epoch)
            tf.summary.scalar('d_loss', float(st.mean(losses_d)), step=epoch)
            tf.summary.scalar('lr', float(lr_v()), step=epoch)
        # dynamic learning rate update
        lr_v.step()

        if (epoch != 0) and (epoch % 10 == 0):
            G.save_weights(os.path.join(checkpoint_dir, 'g_'+str(epoch)+'.npz'), format='npz_dict')
            D.save_weights(os.path.join(checkpoint_dir, 'd_'+str(epoch)+'.npz'), format='npz_dict')

            #validation 
            G.set_eval()

            for i in range(valid_ds_img_nums):
                #imid = random.randint(0,(valid_ds_img_nums-1))

                valid_hr_img = valid_hr_imgs[i]

                #First validate imagage has alwas croped from top left corner
                if i == 0:
                    valid_hr_img = valid_save_crop(valid_hr_img)
                else:
                    valid_hr_img = valid_crop(valid_hr_img)

                valid_lr_img = np.asarray(valid_hr_img)
                hr_size1 = [valid_lr_img.shape[0], valid_lr_img.shape[1]]
                valid_lr_img = cv2.resize(valid_lr_img, dsize=(hr_size1[1] // 4, hr_size1[0] // 4))
                valid_lr_img_tensor = (valid_lr_img / 127.5) - 1  # rescale to ［－1, 1]

                valid_lr_img_tensor = np.asarray(valid_lr_img_tensor, dtype=np.float32)
                valid_lr_img_tensor = np.transpose(valid_lr_img_tensor, axes=[2, 0, 1])
                valid_lr_img_tensor = valid_lr_img_tensor[np.newaxis, :, :, :]
                valid_lr_img_tensor = tlx.ops.convert_to_tensor(valid_lr_img_tensor)
                

                out = tlx.ops.convert_to_numpy(G(valid_lr_img_tensor))
                out = np.asarray((out + 1) * 127.5, dtype=np.uint8)
                out = np.transpose(out[0], axes=[1, 2, 0])

                if i == 0:
                    tlx.vision.save_image(out, file_name = 'valid_gen'+str(epoch)+'_'+str(i)+'.tif',path=save_dir)
                    tlx.vision.save_image(valid_hr_img, file_name = 'valid_hr'+str(epoch)+'_'+str(i)+'.tif', path=save_dir)

                psnr_value = tf.image.psnr(out, valid_hr_img, max_val=255)
                psnr_values.append(float(psnr_value))
                ssim_value = tf.image.ssim(out, valid_hr_img, max_val=255)
                ssim_values.append(float(ssim_value))


            with train_writer.as_default():
                tf.summary.scalar('psnr', float(st.mean(psnr_values)), step=epoch)
                tf.summary.scalar('ssim', float(st.mean(ssim_values)), step=epoch)
                
#            print("Validation --> LR size: %s /  generated HR size: %s" % (size, out.shape))
#            print("PSNR: %s, SSIM: %s" % float(st.mean(psnr_values)), float(st.mean(ssim_values)))

            G.set_train()

def evaluate():
    ###====================== PRE-LOAD DATA ===========================###
    #valid_hr_imgs = tlx.vision.load_images(path=config.VALID.hr_img_path )
    ###========================LOAD WEIGHTS ============================###
    G.load_weights(os.path.join(checkpoint_dir, 'g.npz'), format='npz_dict')
    G.set_eval()
    imid = 0  # 0: 企鹅  81: 蝴蝶 53: 鸟  64: 古堡
    #valid_crop = RandomCrop(size=(2200, 2200))
    valid_hr_img = valid_hr_imgs[imid]
    valid_hr_img = valid_crop(valid_hr_img)
    valid_lr_img = np.asarray(valid_hr_img)
    hr_size1 = [valid_lr_img.shape[0], valid_lr_img.shape[1]]
    valid_lr_img = cv2.resize(valid_lr_img, dsize=(hr_size1[1] // 4, hr_size1[0] // 4))
    valid_lr_img_tensor = (valid_lr_img / 127.5) - 1  # rescale to ［－1, 1]


    valid_lr_img_tensor = np.asarray(valid_lr_img_tensor, dtype=np.float32)
    valid_lr_img_tensor = np.transpose(valid_lr_img_tensor,axes=[2, 0, 1])
    valid_lr_img_tensor = valid_lr_img_tensor[np.newaxis, :, :, :]
    valid_lr_img_tensor= tlx.ops.convert_to_tensor(valid_lr_img_tensor)
    size = [valid_lr_img.shape[0], valid_lr_img.shape[1]]

    out = tlx.ops.convert_to_numpy(G(valid_lr_img_tensor))
    out = np.asarray((out + 1) * 127.5, dtype=np.uint8)
    out = np.transpose(out[0], axes=[1, 2, 0])
    print("LR size: %s /  generated HR size: %s" % (size, out.shape))  # LR size: (339, 510, 3) /  gen HR size: (1, 1356, 2040, 3)
    print("[*] save images")
    tlx.vision.save_image(out, file_name='valid_gen.tif', path=save_dir)
    tlx.vision.save_image(valid_lr_img, file_name='valid_lr.tif', path=save_dir)
    tlx.vision.save_image(valid_hr_img, file_name='valid_hr.tif', path=save_dir)
    out_bicu = cv2.resize(valid_lr_img, dsize = [size[1] * 4, size[0] * 4], interpolation = cv2.INTER_CUBIC)
    tlx.vision.save_image(out_bicu, file_name='valid_hr_cubic.tif', path=save_dir)


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument('--mode', type=str, default='train', help='train, eval')

    args = parser.parse_args()

    tlx.global_flag['mode'] = args.mode

    if tlx.global_flag['mode'] == 'train':
        train()
    elif tlx.global_flag['mode'] == 'eval':
        evaluate()
    else:
        raise Exception("Unknow --mode")
