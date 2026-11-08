#!/bin/bash
cd "$(dirname "$0")"

source "$(conda info --base)/etc/profile.d/conda.sh"
conda activate formants_mak

python formants_mak.py
