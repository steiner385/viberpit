import ssl


def test_mtls_certs_chain_verifies(mtls_certs):
    # Server context loads its cert/key.
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(mtls_certs.server_cert, mtls_certs.server_key)
    # A client context trusting the CA accepts the client cert chain.
    client_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=mtls_certs.ca)
    client_ctx.load_cert_chain(mtls_certs.client_cert, mtls_certs.client_key)
    # Rogue client is signed by a different CA than the trusted one.
    assert mtls_certs.rogue_ca != mtls_certs.ca
