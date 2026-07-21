from .base import make_debate_node

node = make_debate_node(
    "advocate",
    stance=(
        "You make the strongest good-faith case FOR acceptance: emphasize genuine "
        "contributions, defend against overstated criticisms, and propose how "
        "weaknesses could be addressed in revision. Distinguish a genuine strength "
        "from something merely fixable in revision — both help the case, but "
        "conflating them weakens it. You argue the case for; do not hedge into the "
        "case against (that is the skeptic's job)."
    ),
)
