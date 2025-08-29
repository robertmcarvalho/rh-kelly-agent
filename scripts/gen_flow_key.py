import base64
import argparse
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def generate_keys(out_dir: Path) -> tuple[Path, Path, Path]:
    """Generate RSA keypair if not exists and write PEM (priv/pub) and DER (pub)."""
    ensure_dir(out_dir)
    priv_pem = out_dir / "flow_private.pem"
    pub_pem = out_dir / "flow_public.pem"
    pub_der = out_dir / "flow_public.der"

    if priv_pem.exists():
        with open(priv_pem, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048, backend=default_backend())
        with open(priv_pem, "wb") as f:
            f.write(
                key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

    public_key = key.public_key()
    with open(pub_pem, "wb") as f:
        f.write(
            public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
    with open(pub_der, "wb") as f:
        f.write(
            public_key.public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    return priv_pem, pub_pem, pub_der


def sign_file(priv_pem: Path, data_path: Path, out_sig_bin: Path, out_sig_b64: Path) -> None:
    with open(priv_pem, "rb") as f:
        priv = serialization.load_pem_private_key(f.read(), password=None, backend=default_backend())
    data = data_path.read_bytes()
    signature = priv.sign(data, padding.PKCS1v15(), hashes.SHA256())
    out_sig_bin.write_bytes(signature)
    out_sig_b64.write_text(base64.b64encode(signature).decode("ascii"), encoding="utf-8")


def verify_signature(pub_pem: Path, data_path: Path, sig_bin: Path) -> bool:
    with open(pub_pem, "rb") as f:
        pub = serialization.load_pem_public_key(f.read(), backend=default_backend())
    data = data_path.read_bytes()
    sig = sig_bin.read_bytes()
    try:
        pub.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate RSA keys for WhatsApp Flow and sign public key.")
    ap.add_argument("--out", default="secrets", help="Output directory for keys and signatures (default: secrets)")
    ap.add_argument("--sign-input", choices=["der", "pem"], default="der", help="Which public key format to sign (default: der)")
    args = ap.parse_args()

    out_dir = Path(args.out)
    ensure_dir(out_dir)

    priv_pem, pub_pem, pub_der = generate_keys(out_dir)
    data_to_sign = pub_der if args.sign_input == "der" else pub_pem

    sig_bin = out_dir / "flow_public.sig"
    sig_b64 = out_dir / "flow_public.sig.b64"
    sign_file(priv_pem, data_to_sign, sig_bin, sig_b64)
    ok = verify_signature(pub_pem, data_to_sign, sig_bin)

    print("Keys and signature ready.")
    print(f"private_pem: {priv_pem}")
    print(f"public_pem:  {pub_pem}")
    print(f"public_der:  {pub_der}")
    print(f"signature:   {sig_bin} (b64 at {sig_b64})")
    print(f"verify:      {'OK' if ok else 'FAILED'} (RS256 over {args.sign_input.upper()})")
    print("\nNEXT: Paste public_pem content and signature_b64 into the Flow Builder > Assinar chave pública.")


if __name__ == "__main__":
    main()

