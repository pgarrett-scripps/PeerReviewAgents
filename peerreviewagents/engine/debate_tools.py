"""Stdio entry point for the fixed debate-v2 tool profile."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from peerreviewagents.engine.debate_evidence import main

if __name__ == '__main__':
    main(sys.argv[1])
