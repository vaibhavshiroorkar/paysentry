import httpx

from app.connectors.alpha import AlphaConnector
from app.connectors.base import Connector, GatewayResult
from app.connectors.beta import BetaConnector
from app.connectors.stripe import StripeConnector

FLAVOURS = {"alpha": AlphaConnector, "beta": BetaConnector, "stripe": StripeConnector}

__all__ = ["Connector", "GatewayResult", "build_connector"]


def build_connector(name: str, flavour: str, client: httpx.AsyncClient, timeout_sec: float) -> Connector:
    return FLAVOURS[flavour](name, client, timeout_sec)
