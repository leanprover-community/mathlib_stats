#!/bin/bash
cd mathlib

git checkout -q master

while [ -f scripts/nolints.txt ]
do
        echo "$(git rev-parse HEAD):$(grep . scripts/nolints.txt | grep -v '^--' | wc -l)"
        git checkout -q HEAD^
done | tac | sed 's/ script.*//'

git checkout -q master
