# Copyright (C) 2018 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

import hashlib
from typing import List, Tuple, NamedTuple, Union, Iterable, Sequence, Optional

from .util import bfh, bh2u, BitcoinException
from . import constants
from . import ecc
from .crypto import hash_160, hmac_oneshot
from .bitcoin import rev_hex, int_to_hex, EncodeBase58Check, DecodeBase58Check, create_falcon_keypair, tdc_falcon, \
    priv_to_pub, PBKDF2_FALCON_ROUNDS
from .logging import get_logger


_logger = get_logger(__name__)
BIP32_PRIME = 0x80000000
UINT32_MAX = (1 << 32) - 1


def protect_against_invalid_ecpoint(func):
    def func_wrapper(*args):
        child_index = args[-1]
        while True:
            is_prime = child_index & BIP32_PRIME
            try:
                return func(*args[:-1], child_index=child_index)
            except ecc.InvalidECPointException:
                _logger.warning('bip32 protect_against_invalid_ecpoint: skipping index')
                child_index += 1
                is_prime2 = child_index & BIP32_PRIME
                if is_prime != is_prime2: raise OverflowError()
    return func_wrapper


@protect_against_invalid_ecpoint
def CKD_priv(parent_privkey: bytes, parent_chaincode: bytes, child_index: int) -> Tuple[bytes, bytes]:
    """Child private key derivation function (from master private key)
    If n is hardened (i.e. the 32nd bit is set), the resulting private key's
    corresponding public key can NOT be determined without the master private key.
    However, if n is not hardened, the resulting private key's corresponding
    public key can be determined without the master private key.
    """
    if child_index < 0: raise ValueError('the bip32 index needs to be non-negative')
    is_hardened_child = bool(child_index & BIP32_PRIME)
    return _CKD_priv(parent_privkey=parent_privkey,
                     parent_chaincode=parent_chaincode,
                     child_index=bfh(rev_hex(int_to_hex(child_index, 4))),
                     is_hardened_child=is_hardened_child)


def _CKD_priv(parent_privkey: bytes, parent_chaincode: bytes,
              child_index: bytes, is_hardened_child: bool) -> Tuple[bytes, bytes]:
    try:
        pubkey = priv_to_pub(parent_privkey)
    except ecc.InvalidECPointException as e:
        raise BitcoinException('Impossible xprv (not within curve order)') from e
    parent_pubkey = pubkey
    if is_hardened_child:
        data = bytes([0]) + parent_privkey + child_index
    else:
        data = parent_pubkey + child_index
    I = hashlib.pbkdf2_hmac('sha512', data, parent_chaincode, iterations=PBKDF2_FALCON_ROUNDS, dklen=96)
    public_key, child_privkey = tdc_falcon.generate_keypair(I[:48])
    child_chaincode = I[48:]
    return child_privkey, child_chaincode


@protect_against_invalid_ecpoint
def CKD_pub(parent_pubkey: bytes, parent_chaincode: bytes, child_index: int) -> Tuple[bytes, bytes]:
    """Child public key derivation function (from public key only)
    This function allows us to find the nth public key, as long as n is
    not hardened. If n is hardened, we need the master private key to find it.
    """
    if child_index < 0: raise ValueError('the bip32 index needs to be non-negative')
    if child_index & BIP32_PRIME: raise Exception('not possible to derive hardened child from parent pubkey')

    return _CKD_pub(parent_pubkey=parent_pubkey,
                    parent_chaincode=parent_chaincode,
                    child_index=bfh(rev_hex(int_to_hex(child_index, 4))))


# helper function, callable with arbitrary 'child_index' byte-string.
# i.e.: 'child_index' does not need to fit into 32 bits here! (c.f. trustedcoin billing)
def _CKD_pub(parent_pubkey: bytes, parent_chaincode: bytes, child_index: bytes) -> Tuple[bytes, bytes]:
    I = hashlib.pbkdf2_hmac('sha512', parent_pubkey + child_index, parent_chaincode, iterations=PBKDF2_FALCON_ROUNDS, dklen=96)
    public_key, child_privkey = tdc_falcon.generate_keypair(I[:48])
    child_chaincode = I[48:]
    return public_key, child_chaincode


def xprv_header(xtype: str, *, net=None) -> bytes:
    if net is None:
        net = constants.net
    return net.XPRV_HEADERS[xtype].to_bytes(length=4, byteorder="big")


def xpub_header(xtype: str, *, net=None) -> bytes:
    if net is None:
        net = constants.net
    return net.XPUB_HEADERS[xtype].to_bytes(length=4, byteorder="big")


class InvalidMasterKeyVersionBytes(BitcoinException): pass


class BIP32Node(NamedTuple):
    xtype: str
    eckey: Union[ecc.ECPubkey, ecc.ECPrivkey]
    chaincode: bytes
    depth: int = 0
    fingerprint: bytes = b'\x00'*4  # as in serialized format, this is the *parent's* fingerprint
    child_number: bytes = b'\x00'*4

    @classmethod
    def from_xkey(cls, xkey: str, *, net=None) -> 'BIP32Node':
        if net is None:
            net = constants.net
        xkey = DecodeBase58Check(xkey)
        depth = xkey[4]
        fingerprint = xkey[5:9]
        child_number = xkey[9:13]
        chaincode = xkey[13:13 + 48]
        header = int.from_bytes(xkey[0:4], byteorder='big')
        if header in net.XPRV_HEADERS_INV:
            headers_inv = net.XPRV_HEADERS_INV
            is_private = True
        elif header in net.XPUB_HEADERS_INV:
            headers_inv = net.XPUB_HEADERS_INV
            is_private = False
        else:
            raise InvalidMasterKeyVersionBytes(f'Invalid extended key format: {hex(header)}')
        xtype = headers_inv[header]
        if is_private:
            eckey = xkey[13 + 49:]
        else:
            eckey = xkey[13 + 48:]
        return BIP32Node(xtype=xtype,
                         eckey=eckey,
                         chaincode=chaincode,
                         depth=depth,
                         fingerprint=fingerprint,
                         child_number=child_number)

    @classmethod
    def from_rootseed(cls, seed: bytes, *, xtype: str) -> 'BIP32Node':
        salt = bytes.fromhex('aaef2d3f4d77ac66e9c5a6c3d8f921d1')
        key = hashlib.pbkdf2_hmac('sha512', seed, salt, iterations=PBKDF2_FALCON_ROUNDS, dklen=96)
        master_k = key[0:48]
        master_c = key[48:]
        public_key, secret_key = tdc_falcon.generate_keypair(master_k)
        return BIP32Node(xtype=xtype,
                         eckey=secret_key,
                         chaincode=master_c)

    @classmethod
    def from_bytes(cls, b: bytes) -> 'BIP32Node':
        if len(b) != 78:
            raise Exception(f"unexpected xkey raw bytes len {len(b)} != 78")
        xkey = EncodeBase58Check(b)
        return cls.from_xkey(xkey)

    def to_xprv(self, *, net=None) -> str:
        payload = self.to_xprv_bytes(net=net)
        return EncodeBase58Check(payload)

    def to_xprv_bytes(self, *, net=None) -> bytes:
        payload = (xprv_header(self.xtype, net=net) +
                   bytes([self.depth]) +
                   self.fingerprint +
                   self.child_number +
                   self.chaincode +
                   bytes([0]) +
                   self.eckey)
        return payload

    def to_xpub(self, *, net=None) -> str:
        payload = self.to_xpub_bytes(net=net)
        return EncodeBase58Check(payload)

    def to_xpub_bytes(self, *, net=None) -> bytes:
        payload = (xpub_header(self.xtype, net=net) +
                   bytes([self.depth]) +
                   self.fingerprint +
                   self.child_number +
                   self.chaincode +
                   (priv_to_pub(self.eckey) if len(self.eckey) == 1281 else self.eckey))
        return payload

    def to_xkey(self, *, net=None) -> str:
        if self.is_private():
            return self.to_xprv(net=net)
        else:
            return self.to_xpub(net=net)

    def to_bytes(self, *, net=None) -> bytes:
        if self.is_private():
            return self.to_xprv_bytes(net=net)
        else:
            return self.to_xpub_bytes(net=net)

    def convert_to_public(self) -> 'BIP32Node':
        if not self.is_private():
            return self
        pubkey = ecc.ECPubkey(self.eckey.get_public_key_bytes())
        return self._replace(eckey=pubkey)

    def is_private(self) -> bool:
        return isinstance(self.eckey, ecc.ECPrivkey)

    def subkey_at_private_derivation(self, path: Union[str, Iterable[int]]) -> 'BIP32Node':
        if path is None:
            raise Exception("derivation path must not be None")
        if isinstance(path, str):
            path = convert_bip32_path_to_list_of_uint32(path)
        if not path:
            return self
        depth = self.depth
        chaincode = self.chaincode
        privkey = self.eckey
        for child_index in path:
            parent_privkey = privkey
            privkey, chaincode = CKD_priv(privkey, chaincode, child_index)
            depth += 1
        parent_pubkey = priv_to_pub(parent_privkey)
        fingerprint = hash_160(parent_pubkey)[0:4]
        child_number = child_index.to_bytes(length=4, byteorder="big")
        return BIP32Node(xtype=self.xtype,
                         eckey=privkey,
                         chaincode=chaincode,
                         depth=depth,
                         fingerprint=fingerprint,
                         child_number=child_number)

    def subkey_at_public_derivation(self, path: Union[str, Iterable[int]]) -> 'BIP32Node':
        if path is None:
            raise Exception("derivation path must not be None")
        if isinstance(path, str):
            path = convert_bip32_path_to_list_of_uint32(path)
        if not path:
            return self.convert_to_public()
        depth = self.depth
        chaincode = self.chaincode
        pubkey = self.eckey
        for child_index in path:
            parent_pubkey = pubkey
            pubkey, chaincode = CKD_pub(pubkey, chaincode, child_index)
            depth += 1
        fingerprint = hash_160(parent_pubkey)[0:4]
        child_number = child_index.to_bytes(length=4, byteorder="big")
        return BIP32Node(xtype=self.xtype,
                         eckey=pubkey,
                         chaincode=chaincode,
                         depth=depth,
                         fingerprint=fingerprint,
                         child_number=child_number)

    def calc_fingerprint_of_this_node(self) -> bytes:
        """Returns the fingerprint of this node.
        Note that self.fingerprint is of the *parent*.
        """
        # TODO cache this

        return hash_160(priv_to_pub(self.eckey) if len(self.eckey) == 1281 else self.eckey)[0:4]


def xpub_type(x):
    return BIP32Node.from_xkey(x).xtype


def is_xpub(text):
    try:
        node = BIP32Node.from_xkey(text)
        return not node.is_private()
    except:
        return False


def is_xprv(text):
    try:
        node = BIP32Node.from_xkey(text)
        return node.is_private()
    except:
        return False


def xpub_from_xprv(xprv):
    return BIP32Node.from_xkey(xprv).to_xpub()


def convert_bip32_path_to_list_of_uint32(n: str) -> List[int]:
    """Convert bip32 path to list of uint32 integers with prime flags
    m/0/-1/1' -> [0, 0x80000001, 0x80000001]

    based on code in trezorlib
    """
    if not n:
        return []
    if n.endswith("/"):
        n = n[:-1]
    n = n.split('/')
    # cut leading "m" if present, but do not require it
    if n[0] == "m":
        n = n[1:]
    path = []
    for x in n:
        if x == '':
            # gracefully allow repeating "/" chars in path.
            # makes concatenating paths easier
            continue
        prime = 0
        if x.endswith("'") or x.endswith("h"):
            x = x[:-1]
            prime = BIP32_PRIME
        if x.startswith('-'):
            if prime:
                raise ValueError(f"bip32 path child index is signalling hardened level in multiple ways")
            prime = BIP32_PRIME
        child_index = abs(int(x)) | prime
        if child_index > UINT32_MAX:
            raise ValueError(f"bip32 path child index too large: {child_index} > {UINT32_MAX}")
        path.append(child_index)
    return path


def convert_bip32_intpath_to_strpath(path: Sequence[int]) -> str:
    s = "m/"
    for child_index in path:
        if not isinstance(child_index, int):
            raise TypeError(f"bip32 path child index must be int: {child_index}")
        if not (0 <= child_index <= UINT32_MAX):
            raise ValueError(f"bip32 path child index out of range: {child_index}")
        prime = ""
        if child_index & BIP32_PRIME:
            prime = "'"
            child_index = child_index ^ BIP32_PRIME
        s += str(child_index) + prime + '/'
    # cut trailing "/"
    s = s[:-1]
    return s


def is_bip32_derivation(s: str) -> bool:
    try:
        if not (s == 'm' or s.startswith('m/')):
            return False
        convert_bip32_path_to_list_of_uint32(s)
    except:
        return False
    else:
        return True


def normalize_bip32_derivation(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    if not is_bip32_derivation(s):
        raise ValueError(f"invalid bip32 derivation: {s}")
    ints = convert_bip32_path_to_list_of_uint32(s)
    return convert_bip32_intpath_to_strpath(ints)


def is_all_public_derivation(path: Union[str, Iterable[int]]) -> bool:
    """Returns whether all levels in path use non-hardened derivation."""
    if isinstance(path, str):
        path = convert_bip32_path_to_list_of_uint32(path)
    for child_index in path:
        if child_index < 0:
            raise ValueError('the bip32 index needs to be non-negative')
        if child_index & BIP32_PRIME:
            return False
    return True


def root_fp_and_der_prefix_from_xkey(xkey: str) -> Tuple[Optional[str], Optional[str]]:
    """Returns the root bip32 fingerprint and the derivation path from the
    root to the given xkey, if they can be determined. Otherwise (None, None).
    """
    node = BIP32Node.from_xkey(xkey)
    derivation_prefix = None
    root_fingerprint = None
    assert node.depth >= 0, node.depth
    if node.depth == 0:
        derivation_prefix = 'm'
        root_fingerprint = node.calc_fingerprint_of_this_node().hex().lower()
    elif node.depth == 1:
        child_number_int = int.from_bytes(node.child_number, 'big')
        derivation_prefix = convert_bip32_intpath_to_strpath([child_number_int])
        root_fingerprint = node.fingerprint.hex()
    return root_fingerprint, derivation_prefix


def is_xkey_consistent_with_key_origin_info(xkey: str, *,
                                            derivation_prefix: str = None,
                                            root_fingerprint: str = None) -> bool:
    bip32node = BIP32Node.from_xkey(xkey)
    int_path = None
    if derivation_prefix is not None:
        int_path = convert_bip32_path_to_list_of_uint32(derivation_prefix)
    if int_path is not None and len(int_path) != bip32node.depth:
        return False
    if bip32node.depth == 0:
        if bfh(root_fingerprint) != bip32node.calc_fingerprint_of_this_node():
            return False
        if bip32node.child_number != bytes(4):
            return False
    if int_path is not None and bip32node.depth > 0:
        if int.from_bytes(bip32node.child_number, 'big') != int_path[-1]:
            return False
    if bip32node.depth == 1:
        if bfh(root_fingerprint) != bip32node.fingerprint:
            return False
    return True
