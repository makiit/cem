import argparse
import copy
import joblib
import logging
import numpy as np
import os
import torch

from pathlib import Path
from pytorch_lightning import seed_everything

import cem.data.CUB200.cub_loader as cub_data_module
import cem.train.training as training
import cem.train.utils as utils
from cem.data.synthetic_loaders import (
    get_synthetic_data_loader,
    get_synthetic_num_features,
)

################################################################################
## DEFAULT OVERWRITEABLE CONFIGS
################################################################################

CUB_CONFIG = dict(
    cv=5,
    max_epochs=300,
    patience=15,
    batch_size=128,
    emb_size=16,
    extra_dims=0,
    concept_loss_weight=5,
    learning_rate=0.01,
    weight_decay=4e-05,
    weight_loss=True,
    c_extractor_arch="resnet34",
    optimizer="sgd",
    bool=False,
    early_stopping_monitor="val_loss",
    early_stopping_mode="min",
    early_stopping_delta=0.0,
    sampling_percent=1,

    momentum=0.9,
    sigmoidal_prob=False,
    training_intervention_prob=0.0,
    embeding_activation=None,
    intervention_freq=4,
)


def main(
    data_module,
    rerun=False,
    result_dir='results/',
    project_name='',
    activation_freq=0,
    num_workers=8,
    single_frequency_epochs=0,
    global_params=None,
    og_config=None,
    gpu=torch.cuda.is_available(),
):
    seed_everything(42)
    # parameters for data, model, and training
    if og_config is None:
        # Then we use the CUB one as the default
        og_config = CUB_CONFIG
    og_config = copy.deepcopy(og_config)
    og_config['num_workers'] = num_workers
    utils.extend_with_global_params(og_config, global_params or [])

    gpu = 1 if gpu else 0
    utils.extend_with_global_params(og_config, global_params or [])

    train_dl, val_dl, test_dl, imbalance, (n_concepts, n_tasks, _) = data_module.generate_data(
        config=og_config,
        seed=42,
        output_dataset_vars=True,
    )

    if result_dir and activation_freq:
        # Then let's save the testing data for further analysis later on
        out_acts_save_dir = os.path.join(result_dir, "test_embedding_acts")
        Path(out_acts_save_dir).mkdir(parents=True, exist_ok=True)
        for (ds, name) in [
            (test_dl, "test"),
            (val_dl, "val"),
        ]:
            x_total = []
            y_total = []
            c_total = []
            for x, y, c in ds:
                x_total.append(x.cpu().detach())
                y_total.append(y.cpu().detach())
                c_total.append(c.cpu().detach())
            x_inputs = np.concatenate(x_total, axis=0)
            np.save(os.path.join(out_acts_save_dir, f"x_{name}.npy"), x_inputs)

            y_inputs = np.concatenate(y_total, axis=0)
            np.save(os.path.join(out_acts_save_dir, f"y_{name}.npy"), y_inputs)

            c_inputs = np.concatenate(c_total, axis=0)
            np.save(os.path.join(out_acts_save_dir, f"c_{name}.npy"), c_inputs)

    sample = next(iter(train_dl))
    real_sample = []
    for x in sample:
        if isinstance(x, list):
            real_sample += x
        else:
            real_sample.append(x)
    sample = real_sample
    logging.info(f"Training sample shape is: {sample[0].shape}")
    logging.info(f"Training label shape is: {sample[1].shape}")
    logging.info(f"\tNumber of output classes: {n_tasks}")
    logging.info(f"Training concept shape is: {sample[2].shape}")
    logging.info(f"\tNumber of training concepts: {n_concepts}")

    os.makedirs(result_dir, exist_ok=True)
    results = {}
    for split in range(og_config["cv"]):
        print(f'Experiment {split+1}/{og_config["cv"]}')
        results[f'{split}'] = {}

        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptEmbeddingModel"
        config["extra_name"] = f""
        config["sigmoidal_prob"] = True
        config['training_intervention_prob'] = 0.25
        config['emb_size'] = config['emb_size']
        config["embeding_activation"] = "leakyrelu"
        mixed_emb_shared_prob_model,  mixed_emb_shared_prob_test_results = \
            training.train_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        training.update_statistics(
            results[f'{split}'],
            config,
            mixed_emb_shared_prob_model,
            mixed_emb_shared_prob_test_results,
        )

        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptEmbeddingModel"
        config["extra_name"] = f"NoRandInt"
        config["sigmoidal_prob"] = True
        config['training_intervention_prob'] = 0.0  # TURN OFF RandInt
        config['emb_size'] = config['emb_size']
        config["embeding_activation"] = "leakyrelu"
        mixed_emb_shared_prob_model,  mixed_emb_shared_prob_test_results = \
            training.train_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        training.update_statistics(
            results[f'{split}'],
            config,
            mixed_emb_shared_prob_model,
            mixed_emb_shared_prob_test_results,
        )

        # train model *without* embeddings but with extra capacity (concepts
        # are just *fuzzy* scalars and the model also has some extra capacity).
        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptBottleneckModel"
        config["bool"] = False
        config["extra_dims"] = (config['emb_size'] - 1) * n_concepts
        config["extra_name"] = f"FuzzyExtraCapacity_Logit"
        config["bottleneck_nonlinear"] = "leakyrelu"
        config["sigmoidal_extra_capacity"] = False
        config["sigmoidal_prob"] = False
        extra_fuzzy_logit_model, extra_fuzzy_logit_test_results = \
            training.train_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        training.update_statistics(
            results[f'{split}'],
            config,
            extra_fuzzy_logit_model,
            extra_fuzzy_logit_test_results,
        )

        # fuzzy model
        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptBottleneckModel"
        config["extra_name"] = f"Fuzzy"
        config["bool"] = False
        config["extra_dims"] = 0
        config["sigmoidal_extra_capacity"] = False
        config["sigmoidal_prob"] = True
        extra_fuzzy_logit_model, extra_fuzzy_logit_test_results = \
            training.train_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        training.update_statistics(
            results[f'{split}'],
            config,
            extra_fuzzy_logit_model,
            extra_fuzzy_logit_test_results,
        )

        # Bool model
        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptBottleneckModel"
        config["extra_name"] = f"Bool"
        config["bool"] = True
        bool_model, bool_test_results = training.train_model(
            n_concepts=n_concepts,
            n_tasks=n_tasks,
            config=config,
            train_dl=train_dl,
            val_dl=val_dl,
            test_dl=test_dl,
            split=split,
            imbalance=imbalance,
            result_dir=result_dir,
            rerun=rerun,
            project_name=project_name,
            seed=split,
            activation_freq=activation_freq,
            single_frequency_epochs=single_frequency_epochs,
        )
        training.update_statistics(
            results[f'{split}'],
            config,
            bool_model,
            bool_test_results,
        )


        # sequential and independent models
        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptBottleneckModel"
        config["extra_name"] = f""
        config["sigmoidal_prob"] = True
        ind_model, ind_test_results, seq_model, seq_test_results = \
            training.train_independent_and_sequential_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        config["architecture"] = "IndependentConceptBottleneckModel"
        training.update_statistics(
            results[f'{split}'],
            config,
            ind_model,
            ind_test_results,
        )

        config["architecture"] = "SequentialConceptBottleneckModel"
        training.update_statistics(
            results[f'{split}'],
            config,
            seq_model,
            seq_test_results,
        )

        # train vanilla model with more capacity (i.e., no concept supervision)
        # but with ReLU activation
        config = copy.deepcopy(og_config)
        config["architecture"] = "ConceptBottleneckModel"
        config["extra_name"] = f"NoConceptSupervisionReLU_ExtraCapacity"
        config["bool"] = False
        config["extra_dims"] = (config['emb_size'] - 1) * n_concepts
        config["bottleneck_nonlinear"] = "leakyrelu"
        config["concept_loss_weight"] = 0
        extra_vanilla_relu_model, extra_vanilla_relu_test_results = \
            training.train_model(
                n_concepts=n_concepts,
                n_tasks=n_tasks,
                config=config,
                train_dl=train_dl,
                val_dl=val_dl,
                test_dl=test_dl,
                split=split,
                result_dir=result_dir,
                rerun=rerun,
                project_name=project_name,
                seed=split,
                activation_freq=activation_freq,
                single_frequency_epochs=single_frequency_epochs,
                imbalance=imbalance,
            )
        training.update_statistics(
            results[f'{split}'],
            config,
            extra_vanilla_relu_model,
            extra_vanilla_relu_test_results,
        )

        # save results
        joblib.dump(results, os.path.join(result_dir, f'results.joblib'))

    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description=(
            'Runs CEM experiments on the given dataset.'
        ),
    )
    parser.add_argument(
        '--project_name',
        default='',
        help=(
            "Project name used for Weights & Biases monitoring. If not "
            "provided, we will not use W&B'."
        ),
        metavar="name",

    )

    parser.add_argument(
        '--output_dir',
        '-o',
        default=None,
        help=(
            "directory where we will dump our experiment's results. If not "
            "given, then we will use results/{ds_name}/."
        ),
        metavar="path",

    )
    parser.add_argument(
        'dataset',
        choices=['cub', 'celeba', "xor", "trig", "vec", "dot"],
        help=(
            "Dataset to run experiments for. Must be a supported dataset with "
            "a loader."
        ),
        metavar="ds_name",

    )
    parser.add_argument(
        '--rerun',
        '-r',
        default=False,
        action="store_true",
        help=(
            "If set, then we will force a rerun of the entire experiment even "
            "if valid results are found in the provided output directory. "
            "Note that this may overwrite and previous results, so use "
            "with care."
        ),

    )
    parser.add_argument(
        '--activation_freq',
        default=0,
        help=(
            'how frequently, in terms of epochs, should we store the '
            'embedding activations for our validation set. By default we will '
            'not store any activations.'
        ),
        metavar='N',
        type=int,
    )
    parser.add_argument(
        '--single_frequency_epochs',
        default=0,
        help=(
            'how frequently, in terms of epochs, should we store the '
            'embedding activations for our validation set. By default we will '
            'not store any activations.'
        ),
        metavar='N',
        type=int,
    )
    parser.add_argument(
        '--num_workers',
        default=8,
        help=(
            'number of workers used for data feeders. Do not use more workers '
            'than cores in the machine.'
        ),
        metavar='N',
        type=int,
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        default=False,
        help="starts debug mode in our program.",
    )
    parser.add_argument(
        '-p',
        '--param',
        action='append',
        nargs=2,
        metavar=('param_name=value'),
        help=(
            'Allows the passing of a config param that will overwrite '
            'anything passed as part of the config file itself.'
        ),
        default=[],
    )
    args = parser.parse_args()
    if args.dataset == "cub":
        data_module = cub_data_module
        og_config = CUB_CONFIG
        args.output_dir = args.output_dir.format(ds_name="cub")
        args.project_name = args.project_name.format(ds_name="cub")
    elif args.dataset == "celeba":
        data_module = celeba_data_module
        og_config = CELEBA_CONFIG
        args.output_dir = args.output_dir.format(ds_name="celeba")
        args.project_name = args.project_name.format(ds_name="celeba")
    elif args.dataset in ["xor", "vector", "dot", "trig"]:
        data_module = get_synthetic_data_loader(args.dataset)
        args.project_name = args.project_name.format(ds_name=args.dataset)
        input_features = get_synthetic_num_features(args.dataset)
        og_config = SYNTH_CONFIG
        def synth_c_extractor_arch(
            output_dim,
            pretrained=False,
        ):
            if output_dim is None:
                output_dim = 128
            return torch.nn.Sequential(*[
                torch.nn.Linear(input_features, 128),
                torch.nn.LeakyReLU(),
                torch.nn.Linear(128, 128),
                torch.nn.LeakyReLU(),
                torch.nn.Linear(128, output_dim),
            ])
        og_config["c_extractor_arch"] = synth_c_extractor_arch
    else:
        raise ValueError(f"Unsupported dataset {args.dataset}!")
    if args.output_dir is None:
        args.output_dir = f'results/{args.dataset}/'
    main(
        data_module=data_module,
        rerun=args.rerun,
        result_dir=args.output_dir,
        project_name=args.project_name,
        activation_freq=args.activation_freq,
        num_workers=args.num_workers,
        single_frequency_epochs=args.single_frequency_epochs,
        global_params=args.param,
        og_config=og_config,
    )
