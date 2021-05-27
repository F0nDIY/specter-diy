from app import BaseApp
from gui.screens import Menu, InputScreen, Prompt, TransactionScreen
from .screens import WalletScreen, ConfirmWalletScreen
from gui.common import format_addr

import platform
import os
import secp256k1
import hashlib
from binascii import hexlify, unhexlify, a2b_base64, b2a_base64
from bitcoin.psbt import PSBT, DerivationPath
from bitcoin.liquid.pset import PSET
from bitcoin.liquid.networks import NETWORKS
from bitcoin import script, bip32, ec, compact
from bitcoin.liquid.transaction import LSIGHASH as SIGHASH
from bitcoin.liquid.addresses import address as liquid_address
from .wallet import WalletError, Wallet
from .commands import DELETE, EDIT
from io import BytesIO
from bcur import bcur_encode, bcur_decode, bcur_decode_stream, bcur_encode_stream
from helpers import a2b_base64_stream, is_liquid
import gc
import json

SIGN_PSBT = 0x01
ADD_WALLET = 0x02
# verify address from address itself
# and it's index
VERIFY_ADDRESS = 0x03
# show address with certain
# derivation path or descriptor
DERIVE_ADDRESS = 0x04
# sign psbt transaction encoded in bc-ur format
SIGN_BCUR = 0x05
ADD_ASSET = 0x06
DUMP_ASSETS = 0x07

BASE64_STREAM = 0x64
RAW_STREAM = 0xFF

SIGHASH_NAMES = {
    SIGHASH.ALL: "ALL",
    SIGHASH.NONE: "NONE",
    SIGHASH.SINGLE: "SINGLE",
}
# add sighash | anyonecanpay
for sh in list(SIGHASH_NAMES):
    SIGHASH_NAMES[sh | SIGHASH.ANYONECANPAY] = SIGHASH_NAMES[sh] + " | ANYONECANPAY"
# add sighash | rangeproof
for sh in list(SIGHASH_NAMES):
    SIGHASH_NAMES[sh | SIGHASH.RANGEPROOF] = SIGHASH_NAMES[sh] + " | RANGEPROOF"

def get_address(vout, psbtout, network):
    """Helper function to get an address for every output"""
    if is_liquid(network):
        # liquid fee
        if vout.script_pubkey.data == b"":
            return "Fee"
        if psbtout.blinding_pubkey is not None:
            # TODO: check rangeproof if it's present,
            # otherwise generate it ourselves if sighash is | RANGEPROOF
            bpub = ec.PublicKey.parse(psbtout.blinding_pubkey)
            return liquid_address(vout.script_pubkey, bpub, network)
    # finally just return bitcoin address or unconfidential
    try:
        return vout.script_pubkey.address(network)
    except Exception as e:
        # if script doesn't have address representation
        return hexlify(vout.script_pubkey.data).decode()

class WalletManager(BaseApp):
    """
    WalletManager class manages your wallets.
    It stores public information about the wallets
    in the folder and signs it with keystore's id key
    """

    button = "Wallets"
    assets = {
        bytes(reversed(unhexlify("230164e2d3ff2c88cc0739e56a3501c979fe131fd07944e8a609323ef26c6918"))): "tBTC",
    }

    def __init__(self, path):
        self.root_path = path
        platform.maybe_mkdir(path)
        self.path = None
        self.wallets = []

    def init(self, keystore, network, *args, **kwargs):
        """Loads or creates default wallets for new keystore or network"""
        super().init(keystore, network, *args, **kwargs)
        self.keystore = keystore
        # add fingerprint dir
        path = self.root_path + "/" + hexlify(self.keystore.fingerprint).decode()
        platform.maybe_mkdir(path)
        if network not in NETWORKS:
            raise WalletError("Invalid network")
        self.network = network
        # add network dir
        path += "/" + network
        platform.maybe_mkdir(path)
        self.path = path
        self.wallets = self.load_wallets()
        if self.wallets is None or len(self.wallets) == 0:
            w = self.create_default_wallet(path=self.path + "/0")
            self.wallets = [w]
        self.load_assets()

    async def menu(self, show_screen):
        buttons = [(None, "Your wallets")]
        buttons += [(w, w.name) for w in self.wallets if not w.is_watchonly]
        if len(buttons) != (len(self.wallets)+1):
            buttons += [(None, "Watch only wallets")]
            buttons += [(w, w.name) for w in self.wallets if w.is_watchonly]
        menuitem = await show_screen(Menu(buttons, last=(255, None)))
        if menuitem == 255:
            # we are done
            return False
        else:
            w = menuitem
            # pass wallet and network
            self.show_loader(title="Loading wallet...")
            cmd = await w.show(self.network, show_screen)
            if cmd == DELETE:
                scr = Prompt(
                    "Delete wallet?",
                    'You are deleting wallet "%s".\n'
                    "Are you sure you want to do it?" % w.name,
                )
                conf = await show_screen(scr)
                if conf:
                    self.delete_wallet(w)
            elif cmd == EDIT:
                scr = InputScreen(
                    title="Enter new wallet name", note="", suggestion=w.name
                )
                name = await show_screen(scr)
                if name is not None and name != w.name and name != "":
                    w.name = name
                    w.save(self.keystore)
            return True

    def can_process(self, stream):
        cmd, stream = self.parse_stream(stream)
        return cmd is not None

    def parse_stream(self, stream):
        prefix = self.get_prefix(stream)
        # if we have prefix
        if prefix is not None:
            if prefix == b"sign":
                return SIGN_PSBT, stream
            elif prefix == b"showaddr":
                return DERIVE_ADDRESS, stream
            elif prefix == b"addwallet":
                return ADD_WALLET, stream
            elif is_liquid(self.network) and prefix == b"addasset":
                return ADD_ASSET, stream
            elif is_liquid(self.network) and prefix == b"dumpassets":
                return DUMP_ASSETS, stream
            else:
                return None, None
        # if not - we get data any without prefix
        # trying to detect type:
        # probably base64-encoded PSBT
        data = stream.read(40)
        if data[:9] == b"UR:BYTES/":
            # rewind
            stream.seek(0)
            return SIGN_BCUR, stream
        try:
            psbt = a2b_base64(data)
            if psbt[:len(PSBT.MAGIC)] not in [PSBT.MAGIC, PSET.MAGIC]:
                return None, None
            # rewind
            stream.seek(0)
            return SIGN_PSBT, stream
        except:
            pass
        # probably wallet descriptor
        if b"&" in data and b"?" not in data:
            # rewind
            stream.seek(0)
            return ADD_WALLET, stream
        # probably verifying address
        if data.startswith(b"bitcoin:") or data.startswith(b"BITCOIN:") or b"index=" in data:
            if data.startswith(b"bitcoin:") or data.startswith(b"BITCOIN:"):
                stream.seek(8)
            else:
                stream.seek(0)
            return VERIFY_ADDRESS, stream

        return None, None

    async def process_host_command(self, stream, show_screen):
        platform.delete_recursively(self.tempdir)
        cmd, stream = self.parse_stream(stream)
        if cmd == SIGN_PSBT:
            encoding = BASE64_STREAM
            if stream.read(len(PSBT.MAGIC)) in [PSBT.MAGIC, PSET.MAGIC]:
                encoding = RAW_STREAM
            stream.seek(-len(PSBT.MAGIC), 1)
            res = await self.sign_psbt(stream, show_screen, encoding)
            if res is not None:
                obj = {
                    "title": "Transaction is signed!",
                    "message": "Scan it with your wallet",
                }
                return res, obj
            return
        if cmd == SIGN_BCUR:
            # move to the end of UR:BYTES/
            stream.seek(9, 1)
            # move to the end of hash if it's there
            d = stream.read(70)
            if b"/" in d:
                pos = d.index(b"/")
                stream.seek(pos-len(d)+1, 1)
            else:
                stream.seek(-len(d), 1)
            with open(self.tempdir+"/raw", "wb") as f:
                bcur_decode_stream(stream, f)
            gc.collect()
            with open(self.tempdir+"/raw", "rb") as f:
                res = await self.sign_psbt(f, show_screen, encoding=RAW_STREAM)
            platform.delete_recursively(self.tempdir)
            if res is not None:
                data, hsh = bcur_encode(res.read(), upper=True)
                bcur_res = (b"UR:BYTES/" + hsh + "/" + data)
                obj = {
                    "title": "Transaction is signed!",
                    "message": "Scan it with your wallet",
                }
                gc.collect()
                return BytesIO(bcur_res), obj
            return
        elif cmd == ADD_WALLET:
            # read content, it's small
            desc = stream.read().decode().strip()
            w = self.parse_wallet(desc)
            res = await self.confirm_new_wallet(w, show_screen)
            if res:
                self.add_wallet(w)
            return
        elif cmd == VERIFY_ADDRESS:
            data = stream.read().decode().replace("bitcoin:", "")
            # should be of the form addr?index=N or similar
            if "index=" not in data or "?" not in data:
                raise WalletError("Can't verify address with unknown index")
            addr, rest = data.split("?")
            args = rest.split("&")
            idx = None
            for arg in args:
                if arg.startswith("index="):
                    idx = int(arg[6:])
                    break
            w, _ = self.find_wallet_from_address(addr, index=idx)
            await show_screen(WalletScreen(w, self.network, idx))
            return
        elif cmd == DERIVE_ADDRESS:
            arr = stream.read().split(b" ")
            redeem_script = None
            if len(arr) == 2:
                script_type, path = arr
            elif len(arr) == 3:
                script_type, path, redeem_script = arr
            else:
                raise WalletError("Too many arguments")
            paths = [p.decode() for p in path.split(b",")]
            if len(paths) == 0:
                raise WalletError("Invalid path argument")
            res = await self.showaddr(
                paths, script_type, redeem_script, show_screen=show_screen
            )
            return BytesIO(res), {}
        elif cmd == ADD_ASSET:
            arr = stream.read().decode().split(" ")
            if len(arr) != 2:
                raise WalletError("Invalid number of arguments. Usage: addasset <hex_asset> asset_lbl")
            hexasset, assetlbl = arr
            if await show_screen(Prompt("Import asset?",
                    "Asset:\n\n"+format_addr(hexasset, letters=8, words=2)+"\n\nLabel: "+assetlbl)):
                asset = bytes(reversed(unhexlify(hexasset)))
                self.assets[asset] = assetlbl
                self.save_assets()
            return BytesIO(b"success"), {}
        elif cmd == DUMP_ASSETS:
            return BytesIO(self.assets_json()), {}
        else:
            raise WalletError("Unknown command")

    async def sign_psbt(self, stream, show_screen, encoding=BASE64_STREAM):
        PSBTClass = PSET if is_liquid(self.network) else PSBT
        # we sign rangeproofs by default as well, to verify addresses
        sighash = (SIGHASH.ALL | SIGHASH.RANGEPROOF) if is_liquid(self.network) else SIGHASH.ALL

        try:
            if encoding == BASE64_STREAM:
                with open(self.tempdir+"/raw", "wb") as f:
                    # read in chunks, write to ram file
                    a2b_base64_stream(stream, f)
                with open(self.tempdir+"/raw", "rb") as f:
                    psbt = PSBTClass.read_from(f, compress=True)
                # cleanup
                platform.delete_recursively(self.tempdir)
            else:
                psbt = PSBTClass.read_from(stream, compress=True)
        except Exception as e:
            # TODO: not very nice, better to use custom exception on magic
            if e.args[0] == "Invalid PSBT magic":
                raise WalletError("Wrong transaction type! Switch to %s network to sign PSBT!" % ("Bitcoin" if is_liquid(self.network) else "Liquid"))
            else:
                raise e
        psbt.verify()
        # check if all utxos are there and if there are custom sighashes
        custom_sighashes = []
        for i, inp in enumerate(psbt.inputs):
            if (not inp.is_verified) and inp.witness_utxo is None and inp.non_witness_utxo is None:
                raise WalletError("Invalid PSBT - missing previous transaction")
            if inp.sighash_type and inp.sighash_type != sighash:
                custom_sighashes.append((i, inp.sighash_type))

        if len(custom_sighashes) > 0:
            txt = [("Input %d: " % i) + SIGHASH_NAMES[sh]
                    for (i, sh) in custom_sighashes]
            canceltxt = ("Only sign %s" % SIGHASH_NAMES[sighash]) if len(custom_sighashes) != len(psbt.inputs) else "Cancel"
            confirm = await show_screen(Prompt("Warning!",
                "\nCustom SIGHASH flags are used!\n\n"+"\n".join(txt),
                confirm_text="Sign anyway", cancel_text=canceltxt
            ))
            if confirm:
                sighash = None
            else:
                if len(custom_sighashes) == len(psbt.inputs):
                    # nothing to sign
                    return
        wallets, meta = self.parse_psbt(psbt=psbt)
        if len(meta["unknown_assets"]) > 0:
            scr = Prompt(
                "Warning!",
                "\nUnknown asset in the transaction!\n\n\n"
                "Do you want to label them?\n"
                "Otherwise they will be rendered as \"???\"",
            )
            if await show_screen(scr):
                for asset in meta["unknown_assets"]:
                    # return False if the user cancelled
                    scr = InputScreen("Asset\n\n"+format_addr(hexlify(bytes(reversed(asset))).decode(), letters=8, words=2),
                                note="\nChoose a label for unknown asset.\nBetter to keep it short, like LBTC or LDIY")
                    scr.ta.set_pos(190, 350)
                    scr.ta.set_width(100)
                    lbl = await show_screen(scr)
                    if lbl is None:
                        return
                    else:
                        self.assets[asset] = lbl
                self.save_assets()
                wallets, meta = self.parse_psbt(psbt=psbt)
        # there is an unknown wallet
        # wallet is a list of tuples: (wallet, amount)
        if None in [w[0] for w in wallets]:
            scr = Prompt(
                "Warning!",
                "\nUnknown wallet in inputs!\n\n\n"
                "Wallet for some inpunts is unknown! This means we can't verify change addresses.\n\n\n"
                "Hint:\nYou can cancel this transaction and import the wallet by scanning it's descriptor.\n\n\n"
                "Proceed to the transaction confirmation?",
            )
            proceed = await show_screen(scr)
            if not proceed:
                return None
        spends = []
        for w, amount in wallets:
            if w is None:
                name = "Unknown wallet"
            else:
                name = w.name
            spends.append('%.8f BTC\nfrom "%s"' % (amount / 1e8, name))
        title = "Spending:\n" + "\n".join(spends)
        res = await show_screen(TransactionScreen(title, meta))
        if res:
            self.show_loader(title="Signing transaction...")
            if is_liquid(self.network):
                h = hashlib.sha256()
                # fill missing data
                gc.collect()
                for i, out in enumerate(psbt.outputs):
                    self.show_loader(title="Rangeproof %d..." % i)
                    # skip non-confidential
                    if b'\xfc\x07specter\x01' not in out.unknown:
                        if out.range_proof:
                            h.update(compact.to_bytes(len(proof)))
                            h.update(proof)
                            h.update(compact.to_bytes(len(out.surjection_proof)))
                            h.update(out.surjection_proof)
                        else:
                            h.update(b"\x00\x00")
                        continue
                    nonce = out.unknown[b'\xfc\x07specter\x01']
                    if out.nonce_commitment:
                        assert out.nonce_commitment == ec.PrivateKey(nonce).sec()
                    else:
                        out.nonce_commitment = ec.PrivateKey(nonce).sec()
                    pub = secp256k1.ec_pubkey_parse(out.blinding_pubkey)
                    secp256k1.ec_pubkey_tweak_mul(pub, nonce)
                    sec = secp256k1.ec_pubkey_serialize(pub)
                    ecdh_nonce = hashlib.sha256(hashlib.sha256(sec).digest()).digest()
                    vout = psbt.tx.vout[i]
                    proof = secp256k1.rangeproof_sign(
                        ecdh_nonce, vout.value, secp256k1.pedersen_commitment_parse(out.value_commitment),
                        out.value_blinding_factor, vout.asset[1:]+out.asset_blinding_factor,
                        vout.script_pubkey.data, secp256k1.generator_parse(out.asset_commitment))
                    h.update(compact.to_bytes(len(proof)))
                    h.update(proof)
                    h.update(compact.to_bytes(len(out.surjection_proof)))
                    h.update(out.surjection_proof)
                    out.surjection_proof = None
                    del proof
                    gc.collect()
                psbt.tx._hash_outputs_rangeproofs = h.digest()

            self.show_loader(title="Signing now...")
            sigsStart = 0
            for i, inp in enumerate(psbt.inputs):
                sigsStart += len(list(inp.partial_sigs.keys()))
            for w, _ in wallets:
                if w is None:
                    continue
                # fill derivation paths from proprietary fields
                w.update_gaps(psbt=psbt)
                w.save(self.keystore)
                w.fill_psbt(psbt, self.keystore.fingerprint)
                if w.has_private_keys:
                    w.sign_psbt(psbt, sighash)
            self.keystore.sign_psbt(psbt, sighash)
            # remove unnecessary stuff:
            out_psbt = PSBTClass(psbt.tx)
            sigsEnd = 0
            for i, inp in enumerate(psbt.inputs):
                sigsEnd += len(list(inp.partial_sigs.keys()))
                out_psbt.inputs[i].partial_sigs = inp.partial_sigs
            del psbt
            gc.collect()
            if sigsEnd == sigsStart:
                raise WalletError("We didn't add any signatures!\n\n"
                                  "Maybe you forgot to import the wallet?\n\n"
                                  "Scan the wallet descriptor to import it.")
            if encoding == BASE64_STREAM:
                # TODO: also use ram file
                txt = b2a_base64(out_psbt.serialize()).decode().strip()
            else:
                txt = out_psbt.serialize()
            return BytesIO(txt)

    def assets_json(self):
        assets = {}
        # no support for bytes...
        for asset in self.assets:
            assets[hexlify(bytes(reversed(asset))).decode()] = self.assets[asset]
        return json.dumps(assets)

    def save_assets(self):
        path = self.root_path + "/uid" + self.keystore.uid
        platform.maybe_mkdir(path)
        assets = self.assets_json()
        self.keystore.save_aead(path + "/" + self.network, plaintext=assets.encode(), key=self.keystore.userkey)

    def load_assets(self):
        path = self.root_path + "/" + self.keystore.uid
        platform.maybe_mkdir(path)
        if platform.file_exists(path + "/" + self.network):
            _, assets = self.keystore.load_aead(path + "/" + self.network, key=self.keystore.userkey)
            assets = json.loads(assets.decode())
            # no support for bytes...
            for asset in assets:
                self.assets[bytes(reversed(unhexlify(asset)))] = assets[asset]


    async def confirm_new_wallet(self, w, show_screen):
        keys = w.get_key_dicts(self.network)
        for k in keys:
            k["mine"] = self.keystore.owns(k["key"])
        if not any([k["mine"] for k in keys]):
            if not await show_screen(
                    Prompt("Warning!",
                           "None of the keys belong to the device.\n\n"
                           "Are you sure you still want to add the wallet?")):
                return False
        return await show_screen(ConfirmWalletScreen(w.name, w.full_policy, keys, w.is_miniscript))

    async def showaddr(
        self, paths: list, script_type: str, redeem_script=None, show_screen=None
    ) -> str:
        # TODO: update for liquid
        net = NETWORKS[self.network]
        if redeem_script is not None:
            redeem_script = script.Script(unhexlify(redeem_script))
        # first check if we have corresponding wallet:
        address = None
        if redeem_script is not None:
            if script_type == b"wsh":
                address = script.p2wsh(redeem_script).address(net)
            elif script_type == b"sh-wsh":
                address = script.p2sh(script.p2wsh(redeem_script)).address(net)
            elif script_type == b"sh":
                address = script.p2sh(redeem_script).address(net)
            else:
                raise WalletError("Unsupported script type: %s" % script_type)

        else:
            if len(paths) != 1:
                raise WalletError("Invalid number of paths, expected 1")
            path = paths[0]
            if not path.startswith("m/"):
                path = "m" + path[8:]
            derivation = bip32.parse_path(path)
            pub = self.keystore.get_xpub(derivation)
            if script_type == b"wpkh":
                address = script.p2wpkh(pub).address(net)
            elif script_type == b"sh-wpkh":
                address = script.p2sh(script.p2wpkh(pub)).address(net)
            elif script_type == b"pkh":
                address = script.p2pkh(pub).address(net)
            else:
                raise WalletError("Unsupported script type: %s" % script_type)

        w, (branch_idx, idx) = self.find_wallet_from_address(address, paths=paths)
        if show_screen is not None:
            await show_screen(
                WalletScreen(w, self.network, idx, branch_index=branch_idx)
            )
        return address

    def load_wallets(self):
        """Loads all wallets from path"""
        try:
            platform.maybe_mkdir(self.path)
            # Get ids of the wallets.
            # Every wallet is stored in a numeric folder
            wallet_ids = sorted(
                [
                    int(f[0])
                    for f in os.ilistdir(self.path)
                    if f[0].isdigit() and f[1] == 0x4000
                ]
            )
            return [self.load_wallet(self.path + ("/%d" % wid)) for wid in wallet_ids]
        except:
            return []

    def load_wallet(self, path):
        """Loads a wallet with particular id"""
        try:
            # pass path and key for verification
            w = Wallet.from_path(path, self.keystore)
        except Exception as e:
            # if we failed to load -> delete folder and throw an error
            platform.delete_recursively(path, include_self=True)
            raise WalletError("Can't load wallet from %s\n\n:%s" % (path, str(e)))
        return w

    def create_default_wallet(self, path):
        """Creates default p2wpkh wallet with name `Default`"""
        der = "m/84h/%dh/0h" % NETWORKS[self.network]["bip32"]
        xpub = self.keystore.get_xpub(der)
        desc = "wpkh([%s%s]%s/{0,1}/*)" % (
            hexlify(self.keystore.fingerprint).decode(),
            der[1:],
            xpub.to_base58(NETWORKS[self.network]["xpub"]),
        )
        if is_liquid(self.network):
            # bprv = self.keystore.get_blinding_xprv(der)
            # bkey = "[%s%s]%s/{0,1}/*" % (
            #     hexlify(self.keystore.blinding_fingerprint).decode(),
            #     der[1:],
            #     bprv.to_base58(NETWORKS[self.network]["xprv"]),
            # )
            # desc = "blinded(%s,%s)" % (bkey, desc)
            desc = "blinded(slip77(%s),%s)" % (self.keystore.slip77_key, desc)
        w = Wallet.parse("Default&"+desc, path)
        # pass keystore to encrypt data
        w.save(self.keystore)
        platform.sync()
        return w

    def parse_wallet(self, desc):
        w = None
        # trying to find a correct wallet type
        errors = []
        try:
            w = Wallet.parse(desc)
        except Exception as e:
            # raise if only one wallet class is available (most cases)
            raise WalletError("Can't parse descriptor\n\n%s" % str(e))
        if str(w.descriptor) in [str(ww.descriptor) for ww in self.wallets]:
            raise WalletError("Wallet with this descriptor already exists")
        if not w.check_network(NETWORKS[self.network]):
            raise WalletError("Some keys don't belong to the %s network!" % NETWORKS[self.network]["name"])
        return w

    def add_wallet(self, w):
        self.wallets.append(w)
        wallet_ids = sorted(
            [
                int(f[0])
                for f in os.ilistdir(self.path)
                if f[0].isdigit() and f[1] == 0x4000
            ]
        )
        newpath = self.path + ("/%d" % (max(wallet_ids) + 1))
        platform.maybe_mkdir(newpath)
        w.save(self.keystore, path=newpath)

    def delete_wallet(self, w):
        if w not in self.wallets:
            raise WalletError("Wallet not found")
        self.wallets.pop(self.wallets.index(w))
        w.wipe()

    def find_wallet_from_address(self, addr: str, paths=None, index=None):
        # TODO: update for liquid
        if index is not None:
            for w in self.wallets:
                a, _ = w.get_address(index, self.network)
                print(a)
                if a == addr:
                    return w, (0, index)
        if paths is not None:
            # we can detect the wallet from just one path
            p = paths[0]
            if not p.startswith("m"):
                fingerprint = unhexlify(p[:8])
                derivation = bip32.parse_path("m"+p[8:])
            else:
                fingerprint = self.keystore.fingerprint
                derivation = bip32.parse_path(p)
            derivation_path = DerivationPath(fingerprint, derivation)
            for w in self.wallets:
                der = w.descriptor.check_derivation(derivation_path)
                if der is not None:
                    branch_idx, idx = der
                    a, _ = w.get_address(idx, self.network, branch_idx)
                    if a == addr:
                        return w, (branch_idx, idx)
        raise WalletError("Can't find wallet owning address %s" % addr)

    def parse_psbt(self, psbt):
        """Detects a wallet for transaction and returns an object to display"""
        # wallets owning the inputs
        # will be a tuple (wallet, amount)
        # if wallet is not found - (None, amount)
        wallets = []
        amounts = []

        # calculate fee
        fee = psbt.fee()

        if is_liquid(self.network):
            default_asset = None
        else:
            default_asset = "BTC" if self.network == "main" else "tBTC"

        # metadata for GUI
        meta = {
            "inputs": [{"asset": default_asset} for inp in psbt.inputs],
            "outputs": [
                {
                    "address": get_address(out, psbt.outputs[i], NETWORKS[self.network]),
                    "value": out.value,
                    "change": False,
                    "asset": default_asset,
                }
                for i, out in enumerate(psbt.tx.vout)
            ],
            "fee": fee,
            "warnings": [],
            "unknown_assets": [],
        }
        # detect wallet for all inputs
        for i, inp in enumerate(psbt.inputs):
            found = False
            utxo = psbt.utxo(i)
            # value is stored in utxo for btc tx and in unblinded tx vin for liquid
            value = utxo.value if not is_liquid(self.network) else inp.value
            if is_liquid(self.network):
                if inp.asset in self.assets:
                    meta["inputs"][i]["asset"] = self.assets[inp.asset]
                else:
                    if inp.asset not in meta["unknown_assets"]:
                        meta["unknown_assets"].append(inp.asset)
            meta["inputs"][i].update({
                "label": "Unknown wallet",
                "value": value,
                "sighash": SIGHASH_NAMES[inp.sighash_type or SIGHASH.ALL]
            })
            for w in self.wallets:
                if w.owns(psbt.utxo(i), inp.bip32_derivations, inp.witness_script or inp.redeem_script):
                    branch_idx, idx = w.get_derivation(inp.bip32_derivations)
                    meta["inputs"][i]["label"] = w.name
                    if branch_idx == 1:
                        meta["inputs"][i]["label"] += " change %d" % idx
                    elif branch_idx == 0:
                        meta["inputs"][i]["label"] += " #%d" % idx
                    else:
                        meta["inputs"][i]["label"] += " #%d on branch %d" % (idx, branch_idx)
                    if w not in wallets:
                        wallets.append(w)
                        amounts.append(value)
                    else:
                        idx = wallets.index(w)
                        amounts[idx] += value
                    found = True
                    break
            if not found:
                if None not in wallets:
                    wallets.append(None)
                    amounts.append(value)
                else:
                    idx = wallets.index(None)
                    amounts[idx] += value

        if None in wallets:
            meta["warnings"].append("Unknown wallet in input!")
        if len(wallets) > 1:
            warnings.append("Mixed inputs!")

        # check change outputs
        for i, out in enumerate(psbt.outputs):
            vout = psbt.tx.vout[i]
            if is_liquid(self.network):
                asset = vout.asset[1:]
                if asset in self.assets:
                    meta["outputs"][i]["asset"] = self.assets[asset]
                else:
                    if asset not in meta["unknown_assets"]:
                        meta["unknown_assets"].append(asset)
            if is_liquid(self.network) and vout.script_pubkey.data == b"":
                continue
            for w in wallets:
                if w is None:
                    continue
                if out.blinding_pubkey and w.owns(psbt.tx.vout[i],
                        out.bip32_derivations, out.witness_script or out.redeem_script, ec.PublicKey.parse(out.blinding_pubkey)):
                    meta["outputs"][i]["change"] = True
                    meta["outputs"][i]["label"] = w.name
                    break
        # check gap limits
        gaps = [[] + w.gaps if w is not None else [0, 0] for w in wallets]
        # update gaps according to all inputs
        # because if input and output use the same branch (recv / change)
        # it's ok if both are larger than gap limit
        # but differ by less than gap limit
        # (i.e. old wallet is used)
        for inidx, inp in enumerate(psbt.inputs):
            for i, w in enumerate(wallets):
                if w is None:
                    continue
                if w.owns(psbt.utxo(inidx), inp.bip32_derivations, inp.witness_script or inp.redeem_script):
                    branch_idx, idx = w.get_derivation(inp.bip32_derivations)
                    if gaps[i][branch_idx] < idx + type(w).GAP_LIMIT:
                        gaps[i][branch_idx] = idx + type(w).GAP_LIMIT
        # check all outputs if index is ok
        for i, out in enumerate(psbt.outputs):
            if not meta["outputs"][i]["change"]:
                continue
            for j, w in enumerate(wallets):
                if w.owns(psbt.tx.vout[i], out.bip32_derivations, out.witness_script or out.redeem_script):
                    branch_idx, idx = w.get_derivation(out.bip32_derivations)
                    if branch_idx == 1:
                        meta["outputs"][i]["label"] += " change %d" % idx
                    elif branch_idx == 0:
                        meta["outputs"][i]["label"] += " #%d" % idx
                    else:
                        meta["outputs"][i]["label"] += " #%d on branch %d" % (idx, branch_idx)
                    # add warning if idx beyond gap
                    if idx > gaps[j][branch_idx]:
                        meta["warnings"].append(
                            "Address index %d is beyond the gap limit!" % idx
                        )
                        # one warning of this type is enough
                        break
        wallets = [(wallets[i], amounts[i]) for i in range(len(wallets))]
        return wallets, meta

    def wipe(self):
        """Deletes all wallets info"""
        self.wallets = []
        self.path = None
        platform.delete_recursively(self.root_path)
