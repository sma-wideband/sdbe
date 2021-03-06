#!/usr/bin/env python

from subprocess import Popen

HASHPIPE_CMD = "hashpipe -I {0.instance} -p aphids -c {0.cpu_in} vdif_in_{0.in_type}_thread -c ${0.cpu_inout} vdif_inout_{0.inout_type}_thread -c {0.cpu_out} vdif_out_{0.out_type}_thread"

if __name__ == "__main__":

    from argparse import ArgumentParser

    parser = ArgumentParser(description="Start an APHIDS hashpipe instance")

    parser.add_argument("-i", "--instance", metavar="ID", type=int, default=0,
                        help="instance ID to start (default=0)")
    parser.add_argument("--cpu-in", metavar="CPU_IN", type=int, default=1,
                        help="set CPU affinity to CPU_IN for the input thread (default=1)")
    parser.add_argument("--cpu-inout", metavar="CPU_INOUT", type=int, default=2,
                        help="set CPU affinity to CPU_INOUT for the input/output thread (default=2)")
    parser.add_argument("--cpu-out", metavar="CPU_OUT", type=int, default=3,
                        help="set CPU affinity to CPU_OUT for the output thread (default=3)")
    parser.add_argument("--in-type", metavar="IN_TYPE", type=str, default="null", choices=["null", "file", "net"],
                        help="set the type of input thread (default='null')")
    parser.add_argument("--inout-type", metavar="INOUT_TYPE", type=str, default="null", choices=["null", "gpu"],
                        help="set the type of input/output thread (default='null')")
    parser.add_argument("--out-type", metavar="OUT_TYPE", type=str, default="null", choices=["null", "file", "net"],
                        help="set the type of output thread (default='null')")
    parser.add_argument("--stdout", metavar="STDOUT", type=str, default="stdout.log",
                        help="set the filename for output to stdout (default='stdout.log')")
    parser.add_argument("--stderr", metavar="STDERR", type=str, default="stderr.log",
                        help="set the filename for output to stderr (default='stderr.log')")
    args = parser.parse_args()

    with open(args.stdout, "w") as stdout, open(args.stderr, "w") as stderr:
        process = Popen(HASHPIPE_CMD.format(args).split(), stdout=stdout, stderr=stderr)

    with open("/tmp/aphids.{0}.pid".format(args.instance), "w") as pidfile:
        pidfile.write("{0}".format(process.pid))
