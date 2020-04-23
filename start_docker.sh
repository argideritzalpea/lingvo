#!/usr/bin/env bash

LINGVO_DIR="$HOME/lingvo"  # (change to the cloned lingvo directory, e.g. "$HOME/lingvo")
LINGVO_DEVICE="gpu"  # (Leave empty to build and run CPU only docker)
# docker build --tag tensorflow:lingvo $(test "$LINGVO_DEVICE" = "gpu" && echo "--build-arg base_image=nvidia/cuda:10.1-cudnn7-runtime-ubuntu18.04") - < "$LINGVO_DIR/docker/dev.dockerfile"
docker run --rm $(test "$LINGVO_DEVICE" = "gpu" && echo "--runtime=nvidia") -it -v ${LINGVO_DIR}:/tmp/lingvo -v ${HOME}/.gitconfig:/home/${USER}/.gitconfig:ro -p 6006:6006 -p 8888:8888 --name lingvo asr-chris/lingvo:libri100_v0.2 bash
