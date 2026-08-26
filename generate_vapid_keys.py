import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k=ec.generate_private_key(ec.SECP256R1())
priv=k.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
pub=k.public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
b64=lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()
print('VAPID_PUBLIC_KEY='+b64(pub))
print('VAPID_PRIVATE_KEY='+priv.replace('\n','\\n'))
print('Keep the PRIVATE key secret. Put both values in your hosting environment variables.')
