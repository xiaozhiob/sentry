import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from django.db import router, transaction
from django.utils.functional import cached_property

from sentry import analytics
from sentry.coreapi import APIUnauthorized
from sentry.models.apiapplication import ApiApplication
from sentry.models.apigrant import ApiGrant
from sentry.models.apitoken import ApiToken
from sentry.sentry_apps.models.sentry_app import SentryApp
from sentry.sentry_apps.models.sentry_app_installation import SentryAppInstallation
from sentry.sentry_apps.services.app import RpcSentryAppInstallation
from sentry.sentry_apps.token_exchange.util import token_expiration
from sentry.sentry_apps.token_exchange.validator import Validator
from sentry.silo.safety import unguarded_write
from sentry.users.models.user import User

logger = logging.getLogger("sentry.token-exchange")


@dataclass
class GrantExchanger:
    """
    Exchanges a Grant Code for an Access Token
    """

    install: RpcSentryAppInstallation
    code: str
    client_id: str
    user: User

    def run(self):
        with transaction.atomic(using=router.db_for_write(ApiToken)):
            try:
                self._validate()
                token = self._create_token()

                # Once it's exchanged it's no longer valid and should not be
                # exchangeable, so we delete it.
                self._delete_grant()
            except APIUnauthorized:
                logger.info(
                    "grant-exchanger.context",
                    extra={
                        "application_id": self.application.id,
                        "grant_id": self.grant.id,
                    },
                )
                raise
        self.record_analytics()

        return token

    def record_analytics(self) -> None:
        analytics.record(
            "sentry_app.token_exchanged",
            sentry_app_installation_id=self.install.id,
            exchange_type="authorization",
        )

    def _validate(self) -> None:
        Validator(install=self.install, client_id=self.client_id, user=self.user).run()

        if not self._grant_belongs_to_install() or not self._sentry_app_user_owns_grant():
            raise APIUnauthorized("Forbidden grant")

        if not self._grant_is_active():
            raise APIUnauthorized("Grant has already expired")

    def _grant_belongs_to_install(self) -> bool:
        return self.grant.sentry_app_installation.id == self.install.id

    def _sentry_app_user_owns_grant(self) -> bool:
        return self.grant.application.owner == self.user

    def _grant_is_active(self) -> bool:
        return self.grant.expires_at > datetime.now(timezone.utc)

    def _delete_grant(self) -> None:
        # This will cause a set null to trigger which does not need to cascade an outbox
        with unguarded_write(router.db_for_write(ApiGrant)):
            self.grant.delete()

    def _create_token(self) -> ApiToken:
        token = ApiToken.objects.create(
            user=self.user,
            application=self.application,
            scope_list=self.sentry_app.scope_list,
            expires_at=token_expiration(),
        )
        try:
            SentryAppInstallation.objects.get(id=self.install.id).update(api_token=token)
        except SentryAppInstallation.DoesNotExist:
            pass

        return token

    @cached_property
    def grant(self) -> ApiGrant:
        try:
            return (
                ApiGrant.objects.select_related("sentry_app_installation")
                .select_related("application")
                .select_related("application__sentry_app")
                .get(code=self.code)
            )
        except ApiGrant.DoesNotExist:
            raise APIUnauthorized("Could not find grant")

    @property
    def application(self) -> ApiApplication:
        try:
            return self.grant.application
        except ApiApplication.DoesNotExist:
            raise APIUnauthorized("Could not find application")

    @property
    def sentry_app(self) -> SentryApp:
        try:
            return self.application.sentry_app
        except SentryApp.DoesNotExist:
            raise APIUnauthorized("Could not find sentry app")
