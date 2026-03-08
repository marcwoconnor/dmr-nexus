"""
Cluster-native client protocol for HBlink4.

Runs alongside HomeBrew on the same UDP port. Clients are distinguished
by a 4-byte magic prefix: b'CLNT' for native protocol, vs HomeBrew's
b'DMRD', b'RPTL', etc.

Token-based auth: HMAC-SHA256 signed tokens that any cluster server can
validate without server-local state. Enables seamless failover.

Wire format:
  AUTH request:  b'CLNT' + b'AUTH' + 4B repeater_id + passphrase_hash
  AUTH response: b'CLNT' + b'AACK' + token_bytes (on success)
                 b'CLNT' + b'ANAK' + reason (on failure)
  SUBSCRIBE:     b'CLNT' + b'SUBS' + 4B token_hash + subscription_json
  SUB ACK:       b'CLNT' + b'SACK'
  PING:          b'CLNT' + b'PING' + 4B token_hash
  PONG:          b'CLNT' + b'PONG' + cluster_health_json
  DATA:          b'CLNT' + b'DATA' + 4B token_hash + 55B DMRD
  DISCONNECT:    b'CLNT' + b'DISC' + 4B token_hash
"""

import hashlib
import hmac
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Tuple, Any

logger = logging.getLogger(__name__)

# Magic prefix for native protocol packets
NATIVE_MAGIC = b'CLNT'

# Sub-commands (4 bytes each)
CMD_AUTH = b'AUTH'
CMD_AUTH_ACK = b'AACK'
CMD_AUTH_NAK = b'ANAK'
CMD_SUBSCRIBE = b'SUBS'
CMD_SUB_ACK = b'SACK'
CMD_PING = b'PING'
CMD_PONG = b'PONG'
CMD_DATA = b'DATA'
CMD_DISCONNECT = b'DISC'

# Token format version
TOKEN_VERSION = 1

# Token expiry (default 24 hours)
DEFAULT_TOKEN_TTL = 86400


@dataclass
class Token:
    """Signed authentication token — verifiable by any cluster server."""
    repeater_id: int
    slot1_talkgroups: Optional[List[int]]  # None = allow all
    slot2_talkgroups: Optional[List[int]]
    issued_at: float
    expires_at: float
    cluster_id: str
    signature: bytes = b''

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def token_hash(self) -> bytes:
        """4-byte hash for fast per-packet lookup."""
        return hashlib.sha256(self.signature).digest()[:4]

    def to_bytes(self) -> bytes:
        """Serialize token to wire format."""
        payload = json.dumps({
            'v': TOKEN_VERSION,
            'rid': self.repeater_id,
            's1': self.slot1_talkgroups,
            's2': self.slot2_talkgroups,
            'iat': self.issued_at,
            'exp': self.expires_at,
            'cid': self.cluster_id,
        }, separators=(',', ':')).encode('utf-8')
        # [2B payload_len][payload][32B signature]
        return struct.pack('!H', len(payload)) + payload + self.signature

    @classmethod
    def from_bytes(cls, data: bytes) -> Optional['Token']:
        """Deserialize token from wire format."""
        if len(data) < 34:  # 2B len + min payload + 32B sig
            return None
        try:
            payload_len = struct.unpack('!H', data[:2])[0]
            if len(data) < 2 + payload_len + 32:
                return None
            payload = data[2:2 + payload_len]
            signature = data[2 + payload_len:2 + payload_len + 32]
            d = json.loads(payload.decode('utf-8'))
            return cls(
                repeater_id=d['rid'],
                slot1_talkgroups=d['s1'],
                slot2_talkgroups=d['s2'],
                issued_at=d['iat'],
                expires_at=d['exp'],
                cluster_id=d['cid'],
                signature=signature,
            )
        except (json.JSONDecodeError, KeyError, struct.error):
            return None


class TokenManager:
    """Issues and validates tokens using a shared secret."""

    def __init__(self, shared_secret: str, cluster_id: str,
                 token_ttl: float = DEFAULT_TOKEN_TTL):
        self._secret = shared_secret.encode('utf-8')
        self._cluster_id = cluster_id
        self._token_ttl = token_ttl
        # Cache: token_hash (4 bytes) -> Token
        self._cache: Dict[bytes, Token] = {}

    def issue_token(self, repeater_id: int,
                    slot1_talkgroups: Optional[List[int]],
                    slot2_talkgroups: Optional[List[int]]) -> Token:
        """Issue a signed token for a repeater."""
        now = time.time()
        token = Token(
            repeater_id=repeater_id,
            slot1_talkgroups=slot1_talkgroups,
            slot2_talkgroups=slot2_talkgroups,
            issued_at=now,
            expires_at=now + self._token_ttl,
            cluster_id=self._cluster_id,
        )
        # Sign the payload portion
        payload = json.dumps({
            'v': TOKEN_VERSION,
            'rid': token.repeater_id,
            's1': token.slot1_talkgroups,
            's2': token.slot2_talkgroups,
            'iat': token.issued_at,
            'exp': token.expires_at,
            'cid': token.cluster_id,
        }, separators=(',', ':')).encode('utf-8')
        token.signature = hmac.new(self._secret, payload, hashlib.sha256).digest()

        # Cache for fast per-packet lookup
        self._cache[token.token_hash] = token
        return token

    def validate_token(self, token: Token) -> Tuple[bool, str]:
        """Validate a token's signature and expiry.

        Returns (valid, reason).
        """
        if token.expired:
            return False, 'token expired'

        if token.cluster_id != self._cluster_id:
            return False, 'wrong cluster'

        # Recompute signature
        payload = json.dumps({
            'v': TOKEN_VERSION,
            'rid': token.repeater_id,
            's1': token.slot1_talkgroups,
            's2': token.slot2_talkgroups,
            'iat': token.issued_at,
            'exp': token.expires_at,
            'cid': token.cluster_id,
        }, separators=(',', ':')).encode('utf-8')
        expected = hmac.new(self._secret, payload, hashlib.sha256).digest()

        if not hmac.compare_digest(token.signature, expected):
            return False, 'invalid signature'

        # Cache validated token
        self._cache[token.token_hash] = token
        return True, 'ok'

    def validate_token_hash(self, token_hash: bytes) -> Optional[Token]:
        """Fast per-packet validation via cached token_hash.

        Returns the Token if found and not expired, else None.
        """
        token = self._cache.get(token_hash)
        if token and not token.expired:
            return token
        if token and token.expired:
            del self._cache[token_hash]
        return None

    def cleanup_expired(self):
        """Remove expired tokens from cache."""
        expired = [h for h, t in self._cache.items() if t.expired]
        for h in expired:
            del self._cache[h]

    def cache_size(self) -> int:
        return len(self._cache)
