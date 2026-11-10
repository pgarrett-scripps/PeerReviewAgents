"""Stdio entry point for the local review tools."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from peerreviewagents.engine.investigation import main
if __name__=='__main__':main(sys.argv[1])
