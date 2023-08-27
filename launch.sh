#!/usr/bin/env bash
#SBATCH --job-name=cem-cub-experiments
#### Change account below
#SBATCH --account=ddp390
#SBATCH --partition=gpu-shared
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=10
#SBATCH --cpus-per-task=1
#SBATCH --mem=96G
#SBATCH --gpus=1
#SBATCH --time=030:00:00
#SBATCH --output=output.o%j.%N

# load the environments needed

module purge
module load slurm
module load gpu
module load cuda10.2/toolkit/10.2.89
module list

nvidia-smi
nvcc -V

# run your code
python train_cub.py cub -o results
