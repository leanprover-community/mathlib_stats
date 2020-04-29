#!/usr/bin/gnuplot -persist

set title "Lines in scripts/nolints.txt"
set xlabel "git commit"
set ylabel "#lines"
set terminal png 
set output 'docs/nolints.png'
plot "rev_stats.txt" u (column(0)):1:xtic(1) w l title "nolint lines"
