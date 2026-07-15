# generate_cert.py

from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import datetime
import ipaddress
import os

# 🔧 CHANGE THIS TO YOUR LAN IP
LAN_IP = "192.168.0.7"

# ensure cert folder exists
os.makedirs("certs", exist_ok=True)

# -------------------------
# GENERATE PRIVATE KEY
# -------------------------
key = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048
)

# -------------------------
# SUBJECT / ISSUER
# -------------------------
subject = issuer = x509.Name([
    x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ALEX"),
    x509.NameAttribute(NameOID.COMMON_NAME, LAN_IP),
])

# -------------------------
# BUILD CERT
# -------------------------
cert = (
    x509.CertificateBuilder()
    .subject_name(subject)
    .issuer_name(issuer)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.datetime.utcnow())
    .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
    .add_extension(
        x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.IPv4Address(LAN_IP)),
            x509.DNSName("localhost"),
        ]),
        critical=False
    )
    .sign(key, hashes.SHA256())
)

# -------------------------
# SAVE KEY
# -------------------------
with open("certs/key.pem", "wb") as f:
    f.write(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        )
    )

# -------------------------
# SAVE CERT
# -------------------------
with open("certs/cert.pem", "wb") as f:
    f.write(cert.public_bytes(serialization.Encoding.PEM))

print("✅ SSL certificates generated in /certs")