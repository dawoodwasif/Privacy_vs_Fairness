
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import copy
import numpy as np
from torchvision import datasets, transforms
import torch
import argparse
import tenseal as ts
import os

from utils.sampling import mnist_iid, mnist_noniid, cifar_iid
from models.Update import LocalUpdate
from models.Nets import MLP, CNNMnist, CNNCifar
from models.test import test_img

def args_parser():
    parser = argparse.ArgumentParser()
    # federated arguments
    parser.add_argument('--q', type=float, default=10, help="q-FedAvg q value")
    parser.add_argument('--epochs', type=int, default=10, help="rounds of training")
    parser.add_argument('--num_users', type=int, default=100, help="number of users: K")
    parser.add_argument('--frac', type=float, default=0.1, help="the fraction of clients: C")
    parser.add_argument('--local_ep', type=int, default=5, help="the number of local epochs: E")
    parser.add_argument('--local_bs', type=int, default=10, help="local batch size: B")
    parser.add_argument('--bs', type=int, default=128, help="test batch size")
    parser.add_argument('--lr', type=float, default=0.01, help="learning rate")
    parser.add_argument('--momentum', type=float, default=0.5, help="SGD momentum (default: 0.5)")
    parser.add_argument('--split', type=str, default='user', help="train-test split type, user or sample")

    # model arguments
    parser.add_argument('--model', type=str, default='mlp', help='model name')
    parser.add_argument('--kernel_num', type=int, default=9, help='number of each kind of kernel')
    parser.add_argument('--kernel_sizes', type=str, default='3,4,5', help='comma-separated kernel size to use for convolution')
    parser.add_argument('--norm', type=str, default='batch_norm', help="batch_norm, layer_norm, or None")
    parser.add_argument('--num_filters', type=int, default=32, help="number of filters for conv nets")
    parser.add_argument('--max_pool', type=str, default='True', help="Whether use max pooling rather than strided convolutions")

    # other arguments
    parser.add_argument('--dataset', type=str, default='mnist', help="name of dataset")
    parser.add_argument('--iid', action='store_true', help='whether i.i.d or not')
    parser.add_argument('--num_classes', type=int, default=10, help="number of classes")
    parser.add_argument('--num_channels', type=int, default=3, help="number of channels of images")
    parser.add_argument('--gpu', type=int, default=0, help="GPU ID, -1 for CPU")
    parser.add_argument('--stopping_rounds', type=int, default=10, help='rounds of early stopping')
    parser.add_argument('--verbose', action='store_true', help='verbose print')
    parser.add_argument('--seed', type=int, default=1, help='random seed (default: 1)')

    # TenSEAL arguments
    parser.add_argument('--poly_modulus_degree', type=int, default=16384, help="CKKS poly modulus degree")
    parser.add_argument('--coeff_mod_bit_sizes', type=str, default='60,40,40,60', help="CKKS coefficient modulus bit sizes")
    parser.add_argument('--global_scale', type=float, default=2**40, help="Global scale for CKKS encryption")

    return parser.parse_args()

def split_tensor(tensor, max_size):
    """Splits tensor into chunks of size max_size."""
    flat_tensor = tensor.flatten()
    return [flat_tensor[i:i+max_size] for i in range(0, len(flat_tensor), max_size)]

def encrypt_model_weights(weights, context, max_size):
    encrypted_weights = {}
    for key, value in weights.items():
        chunks = split_tensor(value, max_size)
        encrypted_weights[key] = [ts.ckks_vector(context, chunk.tolist()) for chunk in chunks]
    return encrypted_weights

def decrypt_model_weights(encrypted_weights, original_shape, max_size):
    decrypted_weights = {}
    for key, chunks in encrypted_weights.items():
        decrypted_chunks = [torch.tensor(chunk.decrypt()) for chunk in chunks]
        flat_tensor = torch.cat(decrypted_chunks)
        decrypted_weights[key] = flat_tensor.reshape(original_shape[key])
    return decrypted_weights

def euclidean_distance(local_accuracies, global_accuracy):
    return np.sqrt(np.sum((np.array(local_accuracies) - global_accuracy) ** 2))

def predict_and_evaluate(args, net_glob, dataset_train, dataset_test, dict_users):
    # Initialize global model weights
    w_glob = net_glob.state_dict()
    original_shape = {k: v.shape for k, v in w_glob.items()}

    # Initialize TenSEAL context with parameters from arguments
    coeff_mod_bit_sizes = list(map(int, args.coeff_mod_bit_sizes.split(',')))
    context = ts.context(ts.SCHEME_TYPE.CKKS, poly_modulus_degree=args.poly_modulus_degree, coeff_mod_bit_sizes=coeff_mod_bit_sizes)
    context.global_scale = args.global_scale
    context.generate_galois_keys()

    # Determine max size for CKKS vectors
    max_size = args.poly_modulus_degree // 2

    # training
    loss_train = []
    acc_test_rounds = []
    all_rounds_variances = []
    all_rounds_distances = []

    for iter in range(args.epochs):
        w_locals, h_locals, loss_locals, acc_locals = [], [], [], []
        m = max(int(args.frac * args.num_users), 1)
        idxs_users = np.random.choice(range(args.num_users), m, replace=False)
        for idx in idxs_users:
            local = LocalUpdate(args=args, dataset=dataset_train, idxs=dict_users[idx])
            local_net = copy.deepcopy(net_glob).to(args.device)
            w, loss = local.train(net=local_net)

            # Calculate Δw_k^t = L(w^t - \tilde{w}_k^{t+1})
            delta_w_k = copy.deepcopy(w)
            for lk in delta_w_k.keys():
                delta_w_k[lk] = delta_w_k[lk] - w_glob[lk]

            # Calculate L(w^t - \tilde{w}_k^{t+1})
            L_w = 0
            for lk in delta_w_k.keys():
                L_w += torch.norm(delta_w_k[lk]) ** 2

            # Calculate h_k^t
            h_k = args.q * (L_w ** (args.q - 1)) + 1e-10  # Add small constant to avoid division by zero

            # Encrypt local weights
            encrypted_w = encrypt_model_weights(w, context, max_size)

            w_locals.append(copy.deepcopy(encrypted_w))
            h_locals.append(h_k)
            loss_locals.append(copy.deepcopy(loss))

            # Test using the local model post-training
            local_net.eval()
            acc_test_local, loss_test_local = test_img(local_net, dataset_test, args)
            acc_locals.append(acc_test_local)

        # Decrypt and aggregate global weights
        decrypted_w_locals = [decrypt_model_weights(w, original_shape, max_size) for w in w_locals]
        w_glob = copy.deepcopy(decrypted_w_locals[0])
        for lk in w_glob.keys():
            w_glob[lk] = sum(h_k * w[lk] for w, h_k in zip(decrypted_w_locals, h_locals)) / sum(h_locals)

        # copy weight to net_glob
        net_glob.load_state_dict(w_glob)
        net_glob.eval()

        # Calculate and store the variance of this round
        round_variance = np.var(acc_locals)
        all_rounds_variances.append(round_variance)

        # Compute and store average testing accuracy for the round
        round_avg_test_accuracy = np.mean(acc_locals)
        acc_test_rounds.append(round_avg_test_accuracy)

        # Calculate Euclidean Distance
        round_distance = euclidean_distance(acc_locals, round_avg_test_accuracy)
        all_rounds_distances.append(round_distance)

        print("\nRound {:3d}, Local models: \nTesting accuracy average: {:.2f}, Testing accuracy variance: {:.4f}, Euclidean Distance: {:.4f}".format(iter, round_avg_test_accuracy, round_variance, round_distance))

        # Test using the updated global model
        acc_test, loss_test = test_img(net_glob, dataset_test, args)
        acc_test_rounds.append(acc_test)
        print("Round {:3d}, Global model testing accuracy: {:.2f}".format(iter, acc_test))

    # Calculate the final average variance and distance across all rounds
    final_avg_variance = np.mean(all_rounds_variances)
    final_avg_distance = np.mean(all_rounds_distances)
    print("\nFinal Average Variance (AV): {:.4f}".format(final_avg_variance))
    print("Final Average Euclidean Distance (ED): {:.4f}".format(final_avg_distance))

    return net_glob, acc_test_rounds[-1], final_avg_variance, final_avg_distance

if __name__ == '__main__':
    # parse args
    args = args_parser()
    args.device = torch.device('cpu')

    # load dataset and split users
    if args.dataset == 'mnist':
        trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
        dataset_train = datasets.MNIST('../data/mnist/', train=True, download=True, transform=trans_mnist)
        dataset_test = datasets.MNIST('../data/mnist/', train=False, download=True, transform=trans_mnist)
        # sample users
        if args.iid:
            dict_users = mnist_iid(dataset_train, args.num_users)
        else:
            dict_users = mnist_noniid(dataset_train, args.num_users)
        args.num_channels = 1  # MNIST images are grayscale
    elif args.dataset == 'cifar':
        trans_cifar = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])
        dataset_train = datasets.CIFAR10('../data/cifar', train=True, download=True, transform=trans_cifar)
        dataset_test = datasets.CIFAR10('../data/cifar', train=False, download=True, transform=trans_cifar)
        if args.iid:
            dict_users = cifar_iid(dataset_train, args.num_users)
        else:
            exit('Error: only consider IID setting in CIFAR10')
        args.num_channels = 3  # CIFAR-10 images are RGB
    else:
        exit('Error: unrecognized dataset')
    img_size = dataset_train[0][0].shape

    # build model
    if args.model == 'cnn' and args.dataset == 'cifar':
        net_glob = CNNCifar(args=args).to(args.device)
    elif args.model == 'cnn' and args.dataset == 'mnist':
        net_glob = CNNMnist(args=args).to(args.device)
    elif args.model == 'mlp':
        len_in = 1
        for x in img_size:
            len_in *= x
        net_glob = MLP(dim_in=len_in, dim_hidden=64, dim_out=args.num_classes).to(args.device)
    else:
        exit('Error: unrecognized model')
    print(net_glob)

    # Predict and evaluate
    net_glob, final_accuracy, final_variance, final_distance = predict_and_evaluate(args, net_glob, dataset_train, dataset_test, dict_users)

    # Save the model weights
    custom_name = 'q-fedavg_HE_{}'.format(args.q)
    weights_dir = os.path.join('./weights', '{}_{}_{}_{}_degree={}_weights'.format(args.dataset, args.model, args.epochs, custom_name, args.poly_modulus_degree))
    os.makedirs(weights_dir, exist_ok=True)
    weights_filename = os.path.join(weights_dir, 'model_weights_final.pth')
    torch.save(net_glob.state_dict(), weights_filename)
    print(f"Weights saved successfully in {weights_filename}")

    # testing
    net_glob.eval()
    acc_train, loss_train = test_img(net_glob, dataset_train, args)
    acc_test, loss_test = test_img(net_glob, dataset_test, args)
    print("Training accuracy: {:.2f}".format(acc_train))
    print("Testing accuracy: {:.2f}".format(acc_test))
