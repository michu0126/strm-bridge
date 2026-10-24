"""115 wire format, adapted from SheltonZhu/115driver (MIT).

Upstream notice is included in licenses/115driver-LICENSE.txt.
This is the provider's public-key transport format, not a secret application key.
"""
import base64
import secrets

N = int('8686980c0f5a24c4b9d43020cd2c22703ff3f450756529058b1cf88f09b86021'
        '36477198a6e2683149659bd122c33592fdb5ad47944ad1ea4d36c6b172aad633'
        '8c3bb6ac6227502d010993ac967d1aef00f0c8e038de2e4d3bc2ec368af2e9f1'
        '0a6f1eda4f7262f136420c07c331b871bf139f74f3010e3c4fe57df3afb71683', 16)
SEED = bytes.fromhex('f0e569aebfdcbf8a1a45e8be7da673b8de8fe7c445da86c49b648b146ab4f1aa'
                     '3801359e26692c86006b4fa5363462a62a966818f24afdbd6b978f4d8f8913b7'
                     '6c8e93ed0e0d483ed72f88d8fefe7e8650954fd1eb832634db667b9c7e9d7a81'
                     '32eab633de3aa95934663baaba816048b9d5819cf86c8477ff5478265fbee81e'
                     '369f34805c452c9b76d51b8fccc3b8f5')
CLIENT_KEY = bytes.fromhex('7806ad4c33865d184c013f46')


def derive(seed, size):
    return bytes(((seed[i] + SEED[size*i]) & 255) ^ SEED[size*(size-i-1)] for i in range(size))


def xor(data, key):
    mod = len(data) % 4
    return bytes(value ^ key[(i if i < mod else i-mod) % len(key)] for i, value in enumerate(data))


def encode(data, key):
    data = key + xor(xor(data, derive(key, 4))[::-1], CLIENT_KEY)
    out = bytearray()
    for start in range(0, len(data), 117):
        chunk = data[start:start+117]
        padding = bytes(secrets.randbelow(255)+1 for _ in range(125-len(chunk)))
        block = b'\x00\x02' + padding + b'\x00' + chunk
        out.extend(pow(int.from_bytes(block, 'big'), 65537, N).to_bytes(128, 'big'))
    return base64.b64encode(out).decode()


def decode(encoded, key):
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) % 128:
        raise ValueError('invalid 115 response')
    plain = bytearray()
    for start in range(0, len(data), 128):
        block = pow(int.from_bytes(data[start:start+128], 'big'), 65537, N).to_bytes(128, 'big')
        if block[:2] != b'\x00\x01':
            raise ValueError('invalid 115 response padding')
        end = block.find(b'\x00', 2)
        if end < 10:
            raise ValueError('invalid 115 response padding')
        plain.extend(block[end+1:])
    if len(plain) < 16:
        raise ValueError('invalid 115 response length')
    return xor(xor(plain[16:], derive(plain[:16], 12))[::-1], derive(key, 4))
