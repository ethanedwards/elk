"""An ELK reporter network."""

import math
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional, cast

import torch
import torch.nn as nn
from torch import Tensor
from torch.nn.functional import binary_cross_entropy as bce

from ..metrics import roc_auc
from ..parsing import parse_loss
from ..utils.typing import assert_type
from .classifier import Classifier
from .losses import LOSSES
from .reporter import Reporter, ReporterConfig
from .spectral_norm import SpectralNorm


@dataclass
class CcsReporterConfig(ReporterConfig):
    """
    Args:
        activation: The activation function to use. Defaults to GELU.
        bias: Whether to use a bias term in the linear layers. Defaults to True.
        hidden_size: The number of hidden units in the MLP. Defaults to None.
            By default, use an MLP expansion ratio of 4/3. This ratio is used by
            Tucker et al. (2022) <https://arxiv.org/abs/2204.09722> in their 3-layer
            MLP probes. We could also use a ratio of 4, imitating transformer FFNs,
            but this seems to lead to excessively large MLPs when num_layers > 2.
        init: The initialization scheme to use. Defaults to "zero".
        loss: The loss function to use. list of strings, each of the form
            "coef*name", where coef is a float and name is one of the keys in
            `elk.training.losses.LOSSES`.
            Example: --loss 1.0*consistency_squared 0.5*prompt_var
            corresponds to the loss function 1.0*consistency_squared + 0.5*prompt_var.
            Defaults to "ccs_prompt_var".
        normalization: The kind of normalization to apply to the hidden states.
        num_layers: The number of layers in the MLP. Defaults to 1.
        pre_ln: Whether to include a LayerNorm module before the first linear
            layer. Defaults to False.
        supervised_weight: The weight of the supervised loss. Defaults to 0.0.

        lr: The learning rate to use. Ignored when `optimizer` is `"lbfgs"`.
            Defaults to 1e-2.
        num_epochs: The number of epochs to train for. Defaults to 1000.
        num_tries: The number of times to try training the reporter. Defaults to 10.
        optimizer: The optimizer to use. Defaults to "adam".
        weight_decay: The weight decay or L2 penalty to use. Defaults to 0.01.
    """

    activation: Literal["gelu", "relu", "swish"] = "gelu"
    bias: bool = True
    hidden_size: Optional[int] = None
    init: Literal["default", "pca", "spherical", "zero"] = "default"
    loss: list[str] = field(default_factory=lambda: ["ccs"])
    loss_dict: dict[str, float] = field(default_factory=dict, init=False)
    num_layers: int = 1
    pre_ln: bool = False
    supervised_weight: float = 0.0

    lr: float = 1e-2
    num_epochs: int = 1000
    num_tries: int = 10
    optimizer: Literal["adam", "lbfgs"] = "lbfgs"
    weight_decay: float = 0.01

    @classmethod
    def reporter_class(cls) -> type[Reporter]:
        return CcsReporter

    def __post_init__(self):
        self.loss_dict = parse_loss(self.loss)

        # standardize the loss field
        self.loss = [f"{coef}*{name}" for name, coef in self.loss_dict.items()]


class CcsReporter(Reporter):
    """CCS reporter network.

    Args:
        in_features: The number of input features.
        cfg: The reporter configuration.
    """

    config: CcsReporterConfig

    def __init__(
        self,
        cfg: CcsReporterConfig,
        in_features: int,
        device: str | torch.device | None = None,
        dtype: torch.dtype | None = None,
    ):
        super().__init__()
        self.config = cfg
        self.in_features = in_features

        # Learnable Platt scaling parameters
        self.bias = nn.Parameter(torch.zeros(1, device=device, dtype=dtype))
        self.scale = nn.Parameter(torch.ones(1, device=device, dtype=dtype))

        hidden_size = cfg.hidden_size or 4 * in_features // 3

        num_norms = 4
        self.norms = [Normalizer((in_features,), device=device, dtype=dtype) for _ in range(num_norms)]


        self.norm = SpectralNorm(in_features, 1, device=device, dtype=dtype)
        self.probe = nn.Sequential(
            nn.Linear(
                in_features,
                1 if cfg.num_layers < 2 else hidden_size,
                bias=cfg.bias,
                device=device,
            ),
        )
        if cfg.pre_ln:
            self.probe.insert(0, nn.LayerNorm(in_features, elementwise_affine=False))

        act_cls = {
            "gelu": nn.GELU,
            "relu": nn.ReLU,
            "swish": nn.SiLU,
        }[cfg.activation]

        for i in range(1, cfg.num_layers):
            self.probe.append(act_cls())
            self.probe.append(
                nn.Linear(
                    hidden_size,
                    1 if i == cfg.num_layers - 1 else hidden_size,
                    bias=cfg.bias,
                    device=device,
                )
            )

    @torch.no_grad()
    def check_separability(
        self,
        train_pair: tuple[Tensor, Tensor],
        val_pair: tuple[Tensor, Tensor],
    ) -> float:
        """Measure how linearly separable the pseudo-labels are for a contrast pair.

        Args:
            train_pair: A tuple of tensors, (x0, x1), where x0 and x1 are the
                contrastive representations. Used for training the classifier.
            val_pair: A tuple of tensors, (x0, x1), where x0 and x1 are the
                contrastive representations. Used for evaluating the classifier.

        Returns:
            The AUROC of a linear classifier fit on the pseudo-labels.
        """
        x0, x1 = map(self.norm, train_pair)
        val_x0, val_x1 = map(self.norm, val_pair)

        pseudo_clf = Classifier(x0.shape[-1], device=x0.device)  # type: ignore
        pseudo_train = torch.cat(
            [
                torch.zeros_like(x0[..., 0]),
                torch.ones_like(x1[..., 0]),
            ]
        ).flatten()
        pseudo_val = torch.cat(
            [
                torch.zeros_like(val_x0[..., 0]),
                torch.ones_like(val_x1[..., 0]),
            ]
        ).flatten()

        pseudo_clf.fit(
            # b v d -> (b v) d
            torch.cat([x0, x1]).flatten(0, 1),
            pseudo_train,
            # Use the same weight decay as the reporter
            l2_penalty=self.config.weight_decay,
        )
        pseudo_preds = pseudo_clf(
            # b v d -> (b v) d
            torch.cat([val_x0, val_x1]).flatten(0, 1)
        ).squeeze(-1)

        # Edge case where the classifier learns to set its weights to zero
        # Technically AUROC is not defined here but we "fill in" the value of 0.5
        # since this is the limit as the weights approach zero
        if not pseudo_preds.any():
            return 0.5
        else:
            return roc_auc(pseudo_val, pseudo_preds).item()

    def unsupervised_loss(self, logit0: Tensor, logit1: Tensor) -> Tensor:
        loss = sum(
            LOSSES[name](logit0, logit1, coef)
            for name, coef in self.config.loss_dict.items()
        )
        return assert_type(Tensor, loss)

    def reset_parameters(self):
        """Reset the parameters of the probe.

        If init is "spherical", use the spherical initialization scheme.
        If init is "default", use the default PyTorch initialization scheme for
        nn.Linear (Kaiming uniform).
        If init is "zero", initialize all parameters to zero.
        """
        if self.config.init == "spherical":
            # Mathematically equivalent to the unusual initialization scheme used in
            # the original paper. They sample a Gaussian vector of dim in_features + 1,
            # normalize to the unit sphere, then add an extra all-ones dimension to the
            # input and compute the inner product. Here, we use nn.Linear with an
            # explicit bias term, but use the same initialization.
            assert len(self.probe) == 1, "Only linear probes can use spherical init"
            probe = cast(nn.Linear, self.probe[0])  # Pylance gets the type wrong here

            theta = torch.randn(1, probe.in_features + 1, device=probe.weight.device)
            theta /= theta.norm()
            probe.weight.data = theta[:, :-1]
            probe.bias.data = theta[:, -1]

        elif self.config.init == "default":
            for layer in self.probe:
                if isinstance(layer, nn.Linear):
                    layer.reset_parameters()

        elif self.config.init == "zero":
            for param in self.parameters():
                param.data.zero_()
        elif self.config.init != "pca":
            raise ValueError(f"Unknown init: {self.config.init}")

    def forward(self, x: Tensor) -> Tensor:
        """Return the credence assigned to the hidden state `x`."""
        return self.probe(self.norm(x)).squeeze(-1)

    def loss(
        self,
        logit0: Tensor,
        logit1: Tensor,
        labels: Optional[Tensor] = None,
    ) -> Tensor:
        """Return the loss of the reporter on the contrast pair (x0, x1).

        Args:
            logit0: The raw score output of the reporter on x0.
            logit1: The raw score output of the reporter on x1.
            labels: The labels of the contrast pair. Defaults to None.

        Returns:
            loss: The loss of the reporter on the contrast pair (x0, x1).

        Raises:
            ValueError: If `supervised_weight > 0` but `labels` is None.
        """
        loss = self.unsupervised_loss(logit0, logit1)

        # If labels are provided, use them to compute a supervised loss
        if labels is not None:
            num_labels = len(labels)
            assert num_labels <= len(logit0), "Too many labels provided"
            p0 = logit0[:num_labels].sigmoid()
            p1 = logit1[:num_labels].sigmoid()

            alpha = self.config.supervised_weight
            preds = p0.add(1 - p1).mul(0.5).squeeze(-1)
            # broadcast the labels, and flatten the predictions
            # so that both are 1D tensors
            broadcast_labels = labels.repeat_interleave(preds.shape[1]).float()
            flattened_preds = preds.cpu().flatten()
            bce_loss = bce(flattened_preds, broadcast_labels.type_as(flattened_preds))
            loss = alpha * bce_loss + (1 - alpha) * loss

        elif self.config.supervised_weight > 0:
            raise ValueError(
                "Supervised weight > 0 but no labels provided to compute loss"
            )

        return loss

    def fitold(
        self,
        hiddens: Tensor,
        labels: Optional[Tensor] = None,
    ) -> float:
        """Fit the probe to the contrast pair (neg, pos).

        Args:
            contrast_pair: A tuple of tensors, (neg, pos), where x0 and x1 are the
                contrastive representations.
            labels: The labels of the contrast pair. Defaults to None.

        Returns:
            best_loss: The best loss obtained.

        Raises:
            ValueError: If `optimizer` is not "adam" or "lbfgs".
            RuntimeError: If the best loss is not finite.
        """
        x_neg, x_pos = hiddens.unbind(2)

        self.norm.update(x=x_neg, y=torch.zeros_like(x_neg[..., 0]))
        self.norm.update(x=x_pos, y=torch.ones_like(x_pos[..., 0]))
        x_neg, x_pos = self.norm(x_neg), self.norm(x_pos)

        # Record the best acc, loss, and params found so far
        best_loss = torch.inf
        best_state: dict[str, Tensor] = {}  # State dict of the best run

        for i in range(self.config.num_tries):
            self.reset_parameters()

            # This is sort of inefficient but whatever
            if self.config.init == "pca":
                diffs = torch.flatten(x_pos - x_neg, 0, 1)
                _, __, V = torch.pca_lowrank(diffs, q=i + 1)
                self.probe[0].weight.data = V[:, -1, None].T

            if self.config.optimizer == "lbfgs":
                loss = self.train_loop_lbfgs(x_neg, x_pos, labels)
            elif self.config.optimizer == "adam":
                loss = self.train_loop_adam(x_neg, x_pos, labels)
            else:
                raise ValueError(f"Optimizer {self.config.optimizer} is not supported")

            if loss < best_loss:
                best_loss = loss
                best_state = deepcopy(self.state_dict())

        if not math.isfinite(best_loss):
            raise RuntimeError("Got NaN/infinite loss during training")

        self.load_state_dict(best_state)
        return best_loss

    def train_loop_adam(
        self,
        x_neg: Tensor,
        x_pos: Tensor,
        labels: Optional[Tensor] = None,
    ) -> float:
        """Adam train loop, returning the final loss. Modifies params in-place."""

        optimizer = torch.optim.AdamW(
            self.parameters(), lr=self.config.lr, weight_decay=self.config.weight_decay
        )

        loss = torch.inf
        for _ in range(self.config.num_epochs):
            optimizer.zero_grad()

            # We already normalized in fit()
            loss = self.loss(self(x_neg), self(x_pos), labels)
            loss.backward()
            optimizer.step()

        return float(loss)

    def train_loop_lbfgs(
        self,
        x_neg: Tensor,
        x_pos: Tensor,
        labels: Optional[Tensor] = None,
    ) -> float:
        """LBFGS train loop, returning the final loss. Modifies params in-place."""

        optimizer = torch.optim.LBFGS(
            self.parameters(),
            line_search_fn="strong_wolfe",
            max_iter=self.config.num_epochs,
            tolerance_change=torch.finfo(x_pos.dtype).eps,
            tolerance_grad=torch.finfo(x_pos.dtype).eps,
        )
        # Raw unsupervised loss, WITHOUT regularization
        loss = torch.inf

        def closure():
            nonlocal loss
            optimizer.zero_grad()

            # We already normalized in fit()
            loss = self.loss(self(x_neg), self(x_pos), labels)
            regularizer = 0.0

            # We explicitly add L2 regularization to the loss, since LBFGS
            # doesn't have a weight_decay parameter
            for param in self.parameters():
                regularizer += self.config.weight_decay * param.norm() ** 2 / 2

            regularized = loss + regularizer
            regularized.backward()

            return float(regularized)

        optimizer.step(closure)
        return float(loss)


    def fit(
        self,
        hiddens: Tensor,
        labels: Optional[Tensor] = None,
    ) -> float:
        """Fit the probe to the contrast pair (neg, pos).

        Args:
            contrast_pair: A tuple of tensors, (neg, pos), where x0 and x1 are the
                contrastive representations.
            labels: The labels of the contrast pair. Defaults to None.

        Returns:
            best_loss: The best loss obtained.

        Raises:
            ValueError: If `optimizer` is not "adam" or "lbfgs".
            RuntimeError: If the best loss is not finite.
        """
        #print(hiddens)

        #("Hiddens shape is " + str(hiddens.shape))

        # Loop through the third dimension of hiddens and unbind the tensors
        x_tensors = [tensor for tensor in hiddens.unbind(2)]
        print(labels)
        print("Length " + str(len(x_tensors)))
        print("Shape " + str(x_tensors[0].shape))
        # Fit normalizers
        #self.neg_norm.fit(x_neg)
        #self.pos_norm.fit(x_pos)
        #x_neg, x_pos = self.neg_norm(x_neg), self.pos_norm(x_pos)

        for i in range(4):
            #print("fitting")
            self.norms[i].fit(x_tensors[i])
            #print("fitting finished")
        x_tensors = [self.norms[i](x_tensors[i]) for i in range(len(x_tensors))]

        # Record the best acc
        # Record the best acc, loss, and params found so far
        best_loss = torch.inf
        best_state: dict[str, Tensor] = {}  # State dict of the best run

        for i in range(self.config.num_tries):
            self.reset_parameters()

            # This is sort of inefficient but whatever
            if self.config.init == "pca":
                #diffs = torch.flatten(x_pos - x_neg, 0, 1)
                diffs = torch.flatten(1 - torch.sum((x_tensors)), 0, 1)
                _, __, V = torch.pca_lowrank(diffs, q=i + 1)
                self.probe[0].weight.data = V[:, -1, None].T

            if self.config.optimizer == "lbfgs":
                loss = self.trainm_loop_lbfgs(x_tensors, labels)
                #loss = self.train_loop_lbfgs(x_pos, x_neg, labels)
            elif self.config.optimizer == "adam":
                loss = self.trainm_loop_adam(x_tensors, labels)
                #loss = self.train_loop_adam(x_pos, x_neg, labels)
            else:
                raise ValueError(f"Optimizer {self.config.optimizer} is not supported")

            if loss < best_loss:
                best_loss = loss
                best_state = deepcopy(self.state_dict())

        if not math.isfinite(best_loss):
            raise RuntimeError("Got NaN/infinite loss during training")

        self.load_state_dict(best_state)
        return best_loss



    def unsupervised_lossm(self, logits: [Tensor]) -> Tensor:
       
        #print("name of loss function: " + str(name))
        loss = sum(
            LOSSES[name](logits, coef)
            for name, coef in self.config.loss_dict.items()
        )
        
        return assert_type(Tensor, loss)


    def lossm(
        self,
        logits: list[Tensor],
        labels: Optional[Tensor] = None,
    ) -> Tensor:
        """Return the loss of the reporter on the contrast set.

        Args:
            logits: A list of raw score outputs of the reporter, where the first
                element is the positive term and the rest are negative terms.
            labels: The labels of the contrast set. Defaults to None.

        Returns:
            loss: The loss of the reporter on the contrast set.

        Raises:
            ValueError: If `supervised_weight > 0` but `labels` is None.
        """
        loss = self.unsupervised_lossm(logits)

        # If labels are provided, use them to compute a supervised loss
        if labels is not None:
            num_labels = len(labels)
            assert num_labels <= len(logits[0]), "Too many labels provided"
            p0 = logits[0][:num_labels].sigmoid()
            ps = [logit[:num_labels].sigmoid() for logit in logits]


            alpha = self.config.supervised_weight
            stacked_tensors = torch.stack(ps, dim=-1)
            preds, _ = torch.max(stacked_tensors, dim=-1)
            broadcast_labels = labels.repeat_interleave(preds.shape[1]).float()
            #print("blabels " + str(broadcast_labels.shape))
            #print(logits[0][0])
            flattened_preds = preds.cpu().flatten()
            bce_loss = bce(flattened_preds, broadcast_labels.type_as(flattened_preds))
            loss = alpha * bce_loss + (1 - alpha) * loss

        elif self.config.supervised_weight > 0:
            raise ValueError(
                "Supervised weight > 0 but no labels provided to compute loss"
            )

        return loss

    def trainm_loop_adam(
        self,
        x_tensors: [Tensor],
        labels: Optional[Tensor] = None,
    ) -> float:
        """Adam train loop, returning the final loss. Modifies params in-place."""

        optimizer = torch.optim.AdamW(
            self.parameters(), lr=self.config.lr, weight_decay=self.config.weight_decay
        )

        loss = torch.inf
        for _ in range(self.config.num_epochs):
            optimizer.zero_grad()

            loss = self.lossm(self(x_tensors), labels)
            loss.backward()
            optimizer.step()

        return float(loss)


    def trainm_loop_lbfgs(
        self,
        x_tensors: [Tensor],
        labels: Optional[Tensor] = None,
    ) -> float:
        """LBFGS train loop, returning the final loss. Modifies params in-place."""

        optimizer = torch.optim.LBFGS(
            self.parameters(),
            line_search_fn="strong_wolfe",
            max_iter=self.config.num_epochs,
            tolerance_change=torch.finfo(x_tensors[0].dtype).eps,
            tolerance_grad=torch.finfo(x_tensors[0].dtype).eps,
        )
        # Raw unsupervised loss, WITHOUT regularization
        loss = torch.inf

        def closure():
            nonlocal loss
            optimizer.zero_grad()
            correct_tensors = [self(tense) for tense in x_tensors]
            loss = self.lossm(correct_tensors, labels)
            regularizer = 0.0

            # We explicitly add L2 regularization to the loss, since LBFGS
            # doesn't have a weight_decay parameter
            for param in self.parameters():
                regularizer += self.config.weight_decay * param.norm() ** 2 / 2

            regularized = loss + regularizer
            regularized.backward()

            return float(regularized)

        optimizer.step(closure)
        return float(loss)
    def save(self, path: Path | str) -> None:
        """Save the reporter to a file."""
        state = {k: v.cpu() for k, v in self.state_dict().items()}
        state.update(in_features=self.in_features)
        torch.save(state, path)
