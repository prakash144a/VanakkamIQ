from app.verticals.base import Vertical
from app.verticals.restaurant.vertical import RestaurantVertical

VERTICALS: dict[str, Vertical] = {
    "restaurant": RestaurantVertical(),
}


def get_vertical(key: str) -> Vertical:
    try:
        return VERTICALS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown business vertical {key!r}") from exc
