import hashlib

from django.contrib.auth.hashers import BasePasswordHasher, mask_hash
from django.utils.crypto import constant_time_compare


class ToolSecretHasher(BasePasswordHasher):
    """For the secrets `kit tool` generates, never for people's passwords. They're
    long and random, so a slow hash adds nothing. Argon2 would use 100 MB and hold
    the database's write lock for every attempt at the token address, which anyone
    can reach, and that would hold up everyone else's logins."""

    algorithm = "sallskap_sha256"

    def salt(self):
        return ""

    def encode(self, password, salt):
        return f"{self.algorithm}$${hashlib.sha256(password.encode()).hexdigest()}"

    def decode(self, encoded):
        algorithm, salt, digest = encoded.split("$", 2)
        return {"algorithm": algorithm, "salt": salt, "hash": digest}

    def verify(self, password, encoded):
        return constant_time_compare(self.encode(password, ""), encoded)

    def safe_summary(self, encoded):
        return {"algorithm": self.algorithm, "hash": mask_hash(self.decode(encoded)["hash"])}
