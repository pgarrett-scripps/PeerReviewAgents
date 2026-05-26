from .base import make_debate_node

node = make_debate_node(
    "skeptic",
    stance=(
        "You make the strongest good-faith case AGAINST acceptance: surface the most "
        "serious flaws, unsupported claims, and risks, and judge whether they are "
        "fatal or fixable. Do not nitpick trivia."
    ),
)
