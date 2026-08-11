"""Shared fixtures for store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from codify.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from codify.stores.artifact_store.local import LocalArtifactStore
from codify.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from codify.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore


@pytest.fixture()
def agent_store(db_uri: str) -> SqlAlchemyAgentStore:
    """
    :returns: A SqlAlchemyAgentStore backed by the test database.
    """
    return SqlAlchemyAgentStore(db_uri)


@pytest.fixture()
def policy_store(db_uri: str) -> SqlAlchemyPolicyStore:
    """
    :returns: A SqlAlchemyPolicyStore backed by the test database.
    """
    return SqlAlchemyPolicyStore(db_uri)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    :returns: A SqlAlchemyConversationStore backed by the test database.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def split_db_conversation_store(tmp_path: Path) -> SqlAlchemyConversationStore:
    """
    :returns: A SqlAlchemyConversationStore with two separate SQLite databases
        (Codify DB + AP/conversations DB) to exercise split-DB routing.
    """
    codify_uri = f"sqlite:///{tmp_path}/codify.db"
    conv_uri = f"sqlite:///{tmp_path}/conversations.db"
    return SqlAlchemyConversationStore(codify_uri, conv_uri)


@pytest.fixture()
def artifact_store(tmp_path: Path) -> LocalArtifactStore:
    """
    :returns: A LocalArtifactStore in a temp directory.
    """
    return LocalArtifactStore(str(tmp_path / "artifacts"))
