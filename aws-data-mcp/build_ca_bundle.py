#!/usr/bin/env python3
"""
Genera corp-ca-bundle.pem = CAs de certifi + almacen de confianza de Windows.

El proxy corporativo (Zscaler) reemite los certificados TLS con una CA privada,
asi que certifi por si solo rechaza la conexion a AWS. Esa CA privada si esta
instalada en el almacen de Windows, de modo que combinando ambos se puede
mantener la verificacion TLS activa en vez de desactivarla.

Uso:
    python build_ca_bundle.py

Luego apuntar AWS_CA_BUNDLE al archivo generado en .env. Volver a ejecutarlo
si la CA corporativa cambia o caduca.
"""

from __future__ import annotations

import ssl
from pathlib import Path

import certifi

OUTPUT = Path(__file__).resolve().parent / "corp-ca-bundle.pem"


def main() -> None:
    blocks = [Path(certifi.where()).read_text(encoding="utf-8")]
    seen: set[str] = set()
    added = 0

    for store in ("ROOT", "CA"):
        for cert, encoding, _trust in ssl.enum_certificates(store):
            if encoding != "x509_asn":
                continue
            try:
                pem = ssl.DER_cert_to_PEM_cert(cert)
            except ValueError:
                continue
            if pem in seen:
                continue
            seen.add(pem)
            blocks.append(pem)
            added += 1

    OUTPUT.write_text("\n".join(blocks), encoding="utf-8")
    print(f"{OUTPUT} escrito: {added} certificados del almacen de Windows + base de certifi")


if __name__ == "__main__":
    main()
