"""Public daily quest catalog helpers for Near Outpost."""

from __future__ import annotations

from scripts.cora_daily_quests import QUESTS


TAG_LABELS = {
    "animals": "동물",
    "building": "건축",
    "combat": "전투",
    "crafting": "제작",
    "end": "엔드",
    "exploration": "탐험",
    "farming": "농사",
    "fishing": "낚시",
    "food": "음식",
    "gathering": "채집",
    "healing": "힐링",
    "mining": "채굴",
    "movement": "활동",
    "nether": "네더",
    "smelting": "제련",
    "woodcutting": "벌목",
}

DIFFICULTY_LABELS = {
    "easy": "쉬움",
    "normal": "보통",
    "hard": "어려움",
}

BALANCE_MULTIPLIERS = (
    {"range": "0~49,999원", "multiplier": "100%"},
    {"range": "50,000~149,999원", "multiplier": "70%"},
    {"range": "150,000원 이상", "multiplier": "40%"},
)


def public_daily_quest_catalog() -> dict[str, object]:
    """Return the daily quest catalog shape consumed by the public guide."""
    quests = [
        {
            "id": quest.quest_id,
            "title": quest.title,
            "description": quest.description,
            "objective": quest.objective,
            "reward": quest.reward,
            "difficulty": quest.difficulty,
            "difficulty_label": DIFFICULTY_LABELS.get(quest.difficulty, quest.difficulty),
            "tag": quest.tag,
            "tag_label": TAG_LABELS.get(quest.tag, quest.tag),
        }
        for quest in QUESTS
    ]
    return {
        "assignment_count": 3,
        "reset_label": "매일 00:00 KST",
        "reward_currency": "원",
        "balance_multipliers": list(BALANCE_MULTIPLIERS),
        "quests": quests,
        "total_count": len(quests),
    }
