#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.

"""Self Signed X.509 Certificates."""

import logging
import secrets
from typing import Optional

from charms.tls_certificates_interface.v2.tls_certificates import (  # type: ignore[import]
    CertificateCreationRequestEvent,
    TLSCertificatesProvidesV2,
    generate_ca,
    generate_certificate,
    generate_private_key,
)
from ops.charm import CharmBase, ConfigChangedEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, SecretNotFoundError, WaitingStatus

logger = logging.getLogger(__name__)


CA_CERTIFICATES_SECRET_LABEL = "ca-certificates"


class SelfSignedCertificatesCharm(CharmBase):
    """Main class to handle Juju events."""

    def __init__(self, *args):
        """Observes config change and certificate request events."""
        super().__init__(*args)
        self.tls_certificates = TLSCertificatesProvidesV2(self, "certificates")
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.tls_certificates.on.certificate_creation_request,
            self._on_certificate_creation_request,
        )

    @property
    def _config_certificate_validity(self) -> int:
        """Returns certificate validity (in days).

        Returns:
            int: Certificate validity (in days)
        """
        return int(self.model.config.get("certificate-validity", 365))

    @property
    def _config_ca_common_name(self) -> Optional[str]:
        """Returns the user provided common name.

         This common name should only be used when the 'generate-self-signed-certificates' config
         is set to True.

        Returns:
            str: Common name
        """
        return self.model.config.get("ca-common-name", None)

    @property
    def _root_certificate_is_stored(self) -> bool:
        """Returns whether self-signed certificate is stored Juju secret.

        Returns:
            bool: Whether certificates are stored..
        """
        try:
            self.model.get_secret(label=CA_CERTIFICATES_SECRET_LABEL)
            return True
        except SecretNotFoundError:
            return False

    def _generate_root_certificate(self) -> None:
        """Generates root certificate to be used to sign certificates."""
        if not self._config_ca_common_name:
            raise ValueError("CA common name should not be empty")
        private_key_password = generate_password()
        private_key = generate_private_key(password=private_key_password.encode())
        ca_certificate = generate_ca(
            private_key=private_key,
            subject=self._config_ca_common_name,
            private_key_password=private_key_password.encode(),
        )
        self.app.add_secret(
            content={
                "private-key-password": private_key_password,
                "private-key": private_key.decode(),
                "ca-certificate": ca_certificate.decode(),
            },
            label=CA_CERTIFICATES_SECRET_LABEL,
        )
        logger.info("Root certificates generated and stored.")

    def _on_config_changed(self, event: ConfigChangedEvent) -> None:
        """Triggered when the Juju config is changed.

        Args:
            event (ConfigChangedEvent): Juju event.
        """
        if not self.unit.is_leader():
            return
        if invalid_configs := self._invalid_configs():
            self.unit.status = BlockedStatus(
                f"The following configuration values are not valid: {invalid_configs}"
            )
            return
        self._generate_root_certificate()
        self.tls_certificates.revoke_all_certificates()
        logger.info("Revoked all previously issued certificates.")
        self.unit.status = ActiveStatus()

    def _invalid_configs(self) -> list[str]:
        """Returns list of invalid configurations.

        Returns:
            list: List of invalid config keys.
        """
        invalid_configs = []
        if not self._config_ca_common_name:
            invalid_configs.append("ca-common-name")
        if not self._config_certificate_validity:
            invalid_configs.append("certificate-validity")
        return invalid_configs

    def _on_certificate_creation_request(self, event: CertificateCreationRequestEvent) -> None:
        """Handler for certificate requests.

        Args:
            event: CertificateCreationRequestEvent
        """
        if not self.unit.is_leader():
            return
        if invalid_configs := self._invalid_configs():
            self.unit.status = BlockedStatus(
                f"The following configuration values are not valid: {invalid_configs}"
            )
            event.defer()
            return
        if not self._root_certificate_is_stored:
            self.unit.status = WaitingStatus("Root Certificates is not yet generated")
            event.defer()
            return
        ca_certificate_secret = self.model.get_secret(label=CA_CERTIFICATES_SECRET_LABEL)
        ca_certificate_secret_content = ca_certificate_secret.get_content()
        certificate = generate_certificate(
            ca=ca_certificate_secret_content["ca-certificate"].encode(),
            ca_key=ca_certificate_secret_content["private-key"].encode(),
            ca_key_password=ca_certificate_secret_content["private-key-password"].encode(),
            csr=event.certificate_signing_request.encode(),
            validity=self._config_certificate_validity,
        ).decode()
        self.tls_certificates.set_relation_certificate(
            certificate_signing_request=event.certificate_signing_request,
            certificate=certificate,
            ca=ca_certificate_secret_content["ca-certificate"],
            chain=[ca_certificate_secret_content["ca-certificate"], certificate],
            relation_id=event.relation_id,
        )
        logger.info(f"Generated certificate for relation {event.relation_id}")


def generate_password() -> str:
    """Generates a random string containing 64 bytes.

    Returns:
        str: Password
    """
    return secrets.token_hex(64)


if __name__ == "__main__":
    main(SelfSignedCertificatesCharm)
