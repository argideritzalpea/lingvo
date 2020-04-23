#!/usr/bin/env bash

text=""
text+="1: Run gpu test with lingvo\n"
text+="2: Run grapheme model with libri100\n"
printf "$text"

selected=0
while [ ${selected} == 0 ]
do
  read -p "" option
    case ${option} in
      ["1"]* )
        echo "Starting gpu test with lingvo"
        bazel test -j $(nvidia-smi --list-gpus | wc -l) -c opt //lingvo:trainer_test //lingvo:models_test
        selected=1
        ;;
      ["2"]* )
        echo "Attempting to train grapheme model with librispeech 100"
        bazel run -c opt --config=cuda //lingvo:trainer -- --logtostderr \
      --model=asr.librispeech.Librispeech960Grapheme --mode=sync \
      --logdir=/tmp/lingvo/log --saver_max_to_keep=2 \
      --run_locally=gpu 2>&1 |& tee run.log
        selected=1
        ;;
      * ) echo $text;;
      esac
done
