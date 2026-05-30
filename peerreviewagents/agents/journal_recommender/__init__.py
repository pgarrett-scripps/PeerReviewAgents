"""Journal recommender: suggests venues that fit the manuscript's topic + tier.

Runs after the Editor-in-Chief so it can read the final accept/minor/major/
reject verdict and tailor suggestions accordingly (e.g. 'as-is to specialty
journal X' vs 'after the required revisions to top venue Y').
"""

from . import recommender

__all__ = ["recommender"]
