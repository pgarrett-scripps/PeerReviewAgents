"""Author-rebuttal stage.

A single node that plays the manuscript author, defends against the
reviewer panel's critiques, and concedes the ones that genuinely
warrant revision. Sits between the debate and the editor so the
final decision is informed by both sides — the panel's verdict and the
author's defense — rather than the panel alone.
"""

from . import rebuttal

node = rebuttal.node
