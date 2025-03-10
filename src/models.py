from torch.linalg import inv
from src.utils import *


class KalmanFilter(nn.Module):
    """Kalman filter

    x: observation layer
    z: hidden layer
    """

    def __init__(self, A, B, C, Q, R, latent_size) -> None:
        super().__init__()
        self.A = A.clone()
        self.B = B.clone()
        self.C = C.clone()
        # control input, a list/1d array
        self.latent_size = latent_size
        # covariance matrix of noise
        self.Q = Q
        self.R = R

        self.z = None
        self.P = None
        self.x = None
        self.u = None
        self.exs = None

    def projection(self):
        z_proj = torch.matmul(self.A, self.z) + torch.matmul(self.B, self.u)
        P_proj = torch.matmul(self.A, torch.matmul(self.P, self.A.t())) + self.Q
        return z_proj, P_proj

    def correction(self, z_proj, P_proj):
        """Correction step in KF

        K: Kalman gain
        """
        K = torch.matmul(torch.matmul(P_proj, self.C.t()),
                         inv(torch.matmul(torch.matmul(self.C, P_proj), self.C.t()) + self.R))
        self.z = z_proj + torch.matmul(K, self.x - torch.matmul(self.C, z_proj))
        self.P = P_proj - torch.matmul(K, torch.matmul(self.C, P_proj))

    def inference(self, inputs, controls):
        zs = []
        pred_xs = []
        exs = []
        seq_len = inputs.shape[1]
        # initialize mean and covariance estimates of the latent state
        self.z = torch.zeros((self.latent_size, 1)).to(inputs.device)
        self.P = torch.eye(self.latent_size).to(inputs.device)
        for l in range(seq_len):
            self.x = inputs[:, l:l + 1]
            self.u = controls[:, l:l + 1]
            z_proj, P_proj = self.projection()
            self.correction(z_proj, P_proj)
            zs.append(self.z.detach().clone())
            pred_x = torch.matmul(self.C, z_proj)
            pred_xs.append(pred_x)
            exs.append(self.x - pred_x)
        # collect predictions on the observaiton level
        pred_xs = torch.cat(pred_xs, dim=1)
        self.exs = torch.cat(exs, dim=1)
        zs = torch.cat(zs, dim=1)
        return zs, pred_xs


class NeuralKalmanFilter(nn.Module):
    """Aka temporal predictive coding

    x: observation layer
    z: hidden layer
    A, B, C: initial value of weight parameters. In the case of not learning, they are the correct values
    """

    def __init__(self, A, B, C, latent_size, dynamic_inf=False, nonlin='linear') -> None:
        super().__init__()
        self.Wr = A.clone()
        self.Win = B.clone()
        self.Wout = C.clone()

        # control input, a list/1d array
        self.latent_size = latent_size
        self.dynamic_inf = dynamic_inf

        self.ez = None
        self.pred_x = None
        self.ex = None
        self.x = None
        self.u = None
        self.prev_z = None
        self.z = None

        if nonlin == 'linear':
            self.nonlin = Linear()
        elif nonlin == 'tanh':
            self.nonlin = Tanh()
        else:
            raise ValueError("no such nonlinearity!")

    def update_nodes(self, inf_lr):
        with torch.no_grad():
            if self.dynamic_inf:
                # if we use dynamic inference, the prediction is from the previous *internal* inference step
                self.ez = self.z - torch.matmul(self.Wr, self.nonlin(self.z)) - torch.matmul(self.Win,
                                                                                             self.nonlin(self.u))
            else:
                # or esle, the prediction is from the previous *external* time step
                self.ez = self.z - torch.matmul(self.Wr, self.nonlin(self.prev_z)) - torch.matmul(self.Win,
                                                                                                  self.nonlin(self.u))

            # we also need to consider precision here, but for now let's stick with precision=I
            self.pred_x = torch.matmul(self.Wout, self.nonlin(self.z))
            self.ex = self.x - self.pred_x
            delta_z = self.ez - self.nonlin.deriv(self.z) * torch.matmul(self.Wout.t(), self.ex) + 0.0 * torch.sign(
                self.z)
            self.z -= inf_lr * delta_z

    def update_transition(self, learn_lr):
        delta_Wr = torch.matmul(self.ez, self.nonlin(self.prev_z).t())
        self.Wr += learn_lr * delta_Wr

    def update_emission(self, learn_lr):
        # learn the emission matrix
        delta_Wout = torch.matmul(self.ex, self.nonlin(self.z).t())
        self.Wout += learn_lr * delta_Wout

    def predict(self, inputs, controls, inf_iters, inf_lr):
        """Given weigth matrices A and C, this function estimates the latent and observed activities

        A and C can be:
            - Set to true values
            - Learned using the train() function below
            - Totally random
        """

        zs = []  # inferred latent states
        z_projs = []  # projected latent states
        seq_len = inputs.shape[1]

        # initialize the latent states with 0
        self.z = torch.zeros((self.latent_size, 1)).to(inputs.device)
        for l in range(seq_len):
            self.x = inputs[:, l:l + 1]
            self.u = controls[:, l:l + 1]
            self.prev_z = self.z.clone()
            z_projs.append(torch.matmul(self.Wr, self.nonlin(self.z)) + torch.matmul(self.Win, self.nonlin(self.u)))

            # perform inference
            if inf_iters == 0:
                # equilibrium of PC inference, only applies to linear case
                temp1 = torch.linalg.inv(torch.eye(self.latent_size) + torch.matmul(self.Wout.t(), self.Wout))
                temp2 = torch.matmul(self.Wout.t(), self.x) + torch.matmul(self.Wr, self.prev_z) + torch.matmul(
                    self.Win, self.u)
                self.z = torch.matmul(temp1, temp2)
            else:
                # perform inference iteratively
                for itr in range(inf_iters):
                    self.update_nodes(inf_lr)

            zs.append(self.z.detach().clone())

        zs = torch.cat(zs, dim=1)
        z_projs = torch.cat(z_projs, dim=1)
        # make prediction of the observations by a forward pass
        pred_xs = torch.matmul(self.Wout, self.nonlin(z_projs))
        return zs, pred_xs

    def train(self, inputs, controls, inf_iters, inf_lr, learn_iters=1, learn_lr=2e-4):
        """Learn the model weigths A and C"""
        seq_len = inputs.shape[1]

        # initialize the latent states with 0
        for i in range(learn_iters):
            self.z = torch.zeros((self.latent_size, 1)).to(inputs.device)
            for l in range(seq_len):
                self.x = inputs[:, l:l + 1]
                self.u = controls[:, l:l + 1]
                self.prev_z = self.z.clone()

                # perform inference iteratively
                for itr in range(inf_iters):
                    self.update_nodes(inf_lr)

                # update the transition after inference converges
                self.update_transition(learn_lr)

                # update the emission after inference converges
                self.update_emission(learn_lr)


class TemporalPC(nn.Module):
    def __init__(self, control_size, hidden_size, output_size, nonlin='tanh'):
        """A more concise and pytorchy way of implementing tPC

        Suitable for image sequences
        """
        super(TemporalPC, self).__init__()
        self.hidden_size = hidden_size
        self.Win = nn.Linear(control_size, hidden_size, bias=False)
        self.Wr = nn.Linear(hidden_size, hidden_size, bias=False)
        self.Wout = nn.Linear(hidden_size, output_size, bias=False)

        self.hidden_loss = None
        self.obs_loss = None
        self.z = None

        if nonlin == 'linear':
            self.nonlin = Linear()
        elif nonlin == 'tanh':
            self.nonlin = Tanh()
        else:
            raise ValueError("no such nonlinearity!")

    def forward(self, u, prev_z):
        pred_z = self.Win(self.nonlin(u)) + self.Wr(self.nonlin(prev_z))
        pred_x = self.Wout(self.nonlin(pred_z))
        return pred_z, pred_x

    def init_hidden(self, bsz):
        """Initializing prev_z"""
        return nn.init.kaiming_uniform_(torch.empty(bsz, self.hidden_size))

    def update_errs(self, x, u, prev_z):
        pred_z, _ = self.forward(u, prev_z)
        pred_x = self.Wout(self.nonlin(self.z))
        err_z = self.z - pred_z
        err_x = x - pred_x
        return err_z, err_x

    def update_nodes(self, x, u, prev_z, inf_lr, update_x=False):
        err_z, err_x = self.update_errs(x, u, prev_z)
        delta_z = err_z - self.nonlin.deriv(self.z) * torch.matmul(err_x, self.Wout.weight.detach().clone())
        self.z -= inf_lr * delta_z
        if update_x:
            delta_x = err_x
            x -= inf_lr * delta_x

    def inference(self, inf_iters, inf_lr, x, u, prev_z, update_x=False):
        """prev_z should be set up outside the inference, from the previous timestep

        Args:
            train: determines whether we are at the training or inference stage
        
        After every time step, we change prev_z to self.z
        """
        with torch.no_grad():
            # initialize the current hidden state with a forward pass
            self.z, _ = self.forward(u, prev_z)

            # update the values nodes
            for i in range(inf_iters):
                self.update_nodes(x, u, prev_z, inf_lr, update_x)

    def update_grads(self, x, u, prev_z):
        """x: input at a particular timestep in stimulus
        
        Could add some sparse penalty to weights
        """
        err_z, err_x = self.update_errs(x, u, prev_z)
        self.hidden_loss = torch.sum(err_z ** 2)
        self.obs_loss = torch.sum(err_x ** 2)
        energy = self.hidden_loss + self.obs_loss
        return energy
