# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Long-Horizon Agent Memory from BigQuery Traces.

This module provides memory services for ADK agents using historical trace
data stored in BigQuery. It enables:

- Cross-session context retrieval
- Semantic search over past interactions (episodic memory)
- User profile building from trace history
- Context management to prevent cognitive overload

Example usage:
    memory_service = BigQueryMemoryService(
        project_id="my-project",
        dataset_id="agent_analytics",
        table_id="agent_events",
    )

    # Retrieve relevant past context
    memories = await memory_service.search_memory(
        app_name="my_agent",
        user_id="user-123",
        query="weather forecast preferences",
    )
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
from typing import Any, Optional

from google.adk.memory.base_memory_service import BaseMemoryService
from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.adk.sessions.session import Session
from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


@dataclass
class Episode:
  """Represents a past interaction episode from trace data."""

  session_id: str
  timestamp: datetime
  user_message: str
  agent_response: Optional[str]
  tool_calls: list[str] = field(default_factory=list)
  similarity_score: Optional[float] = None
  metadata: dict[str, Any] = field(default_factory=dict)

  def to_memory_entry(self) -> MemoryEntry:
    """Converts episode to MemoryEntry format."""
    from google.genai import types

    content_parts = []
    if self.user_message:
      content_parts.append(types.Part(text=f"User: {self.user_message}"))
    if self.agent_response:
      content_parts.append(types.Part(text=f"Agent: {self.agent_response}"))

    return MemoryEntry(
        content=types.Content(parts=content_parts, role="user"),
        custom_metadata={
            "session_id": self.session_id,
            "tool_calls": self.tool_calls,
            "similarity_score": self.similarity_score,
            **self.metadata,
        },
        timestamp=self.timestamp.isoformat() if self.timestamp else None,
    )


class UserProfile(BaseModel):
  """User profile built from trace history."""

  user_id: str = Field(description="The user identifier.")
  topics_of_interest: list[str] = Field(
      default_factory=list,
      description="Topics the user frequently asks about.",
  )
  communication_style: Optional[str] = Field(
      default=None,
      description="Detected communication style preferences.",
  )
  common_requests: list[str] = Field(
      default_factory=list,
      description="Common types of requests from this user.",
  )
  preferred_tools: list[str] = Field(
      default_factory=list,
      description="Tools frequently used in user's sessions.",
  )
  session_count: int = Field(
      default=0,
      description="Total number of sessions for this user.",
  )
  last_interaction: Optional[datetime] = Field(
      default=None,
      description="Timestamp of last interaction.",
  )
  custom_metadata: dict[str, Any] = Field(
      default_factory=dict,
      description="Additional extracted metadata.",
  )


class BigQuerySessionMemory:
  """Session memory backed by BigQuery traces.

  Enables cross-session context retrieval for the same user, allowing
  agents to maintain continuity across conversations.
  """

  _RECENT_CONTEXT_QUERY = """
  WITH recent_sessions AS (
    SELECT DISTINCT session_id, MIN(timestamp) as start_time
    FROM `{project}.{dataset}.{table}`
    WHERE user_id = @user_id
      AND session_id != @current_session
    GROUP BY session_id
    ORDER BY start_time DESC
    LIMIT @lookback_sessions
  )
  SELECT
    e.session_id,
    e.event_type,
    e.timestamp,
    JSON_EXTRACT_SCALAR(e.content, '$.text_summary') as content_summary,
    JSON_EXTRACT_SCALAR(e.content, '$.response') as response,
    e.agent
  FROM `{project}.{dataset}.{table}` e
  JOIN recent_sessions rs ON e.session_id = rs.session_id
  WHERE e.event_type IN (
    'USER_MESSAGE_RECEIVED', 'LLM_RESPONSE', 'AGENT_COMPLETED'
  )
  ORDER BY e.timestamp DESC
  LIMIT @max_events
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
  ) -> None:
    """Initializes BigQuerySessionMemory.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        client: Optional BigQuery client.
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self._client = client
    self._warned_unlabeled_client = False

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def get_recent_context(
      self,
      user_id: str,
      current_session_id: str,
      lookback_sessions: int = 5,
      max_events: int = 50,
  ) -> list[Episode]:
    """Retrieves recent context from past sessions.

    Args:
        user_id: The user identifier.
        current_session_id: Current session to exclude.
        lookback_sessions: Number of past sessions to consider.
        max_events: Maximum events to return.

    Returns:
        List of Episode objects from past sessions.
    """
    query = self._RECENT_CONTEXT_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
            bigquery.ScalarQueryParameter(
                "current_session", "STRING", current_session_id
            ),
            bigquery.ScalarQueryParameter(
                "lookback_sessions", "INT64", lookback_sessions
            ),
            bigquery.ScalarQueryParameter("max_events", "INT64", max_events),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="memory")

    loop = asyncio.get_event_loop()
    query_job = await loop.run_in_executor(
        None,
        lambda: self.client.query(query, job_config=job_config),
    )
    results = await loop.run_in_executor(None, lambda: list(query_job.result()))

    # Group events by session and construct episodes
    episodes: dict[str, Episode] = {}
    for row in results:
      session_id = row.get("session_id")
      if session_id not in episodes:
        episodes[session_id] = Episode(
            session_id=session_id,
            timestamp=row.get("timestamp", datetime.now(timezone.utc)),
            user_message="",
            agent_response=None,
        )

      event_type = row.get("event_type")
      if event_type == "USER_MESSAGE_RECEIVED":
        content = row.get("content_summary") or ""
        if content and not episodes[session_id].user_message:
          episodes[session_id].user_message = content
      elif event_type in ("LLM_RESPONSE", "AGENT_COMPLETED"):
        response = row.get("response") or row.get("content_summary")
        if response and not episodes[session_id].agent_response:
          episodes[session_id].agent_response = response

    return list(episodes.values())


class BigQueryEpisodicMemory:
  """Episodic memory retrieves relevant past interactions.

  Uses semantic similarity search over embedded traces to find
  interactions similar to the current query.
  """

  _SIMILARITY_SEARCH_QUERY = """
  SELECT
    session_id,
    content,
    timestamp,
    user_id,
    event_type
  FROM `{project}.{dataset}.{table}`
  WHERE user_id = @user_id
    AND event_type = 'USER_MESSAGE_RECEIVED'
    AND timestamp > @since_timestamp
  ORDER BY timestamp DESC
  LIMIT @limit
  """

  # Query for vector similarity search (requires embeddings table)
  _VECTOR_SEARCH_QUERY = """
  SELECT
    base.session_id,
    base.content,
    base.timestamp,
    ML.DISTANCE(base.embedding, @query_embedding, 'COSINE') as distance
  FROM `{project}.{dataset}.{embeddings_table}` base
  WHERE base.user_id = @user_id
    AND base.embedding IS NOT NULL
  ORDER BY distance ASC
  LIMIT @top_k
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      embeddings_table_id: Optional[str] = None,
      client: Optional[bigquery.Client] = None,
      embedding_model: Optional[str] = None,
  ) -> None:
    """Initializes BigQueryEpisodicMemory.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID for events.
        embeddings_table_id: Table with pre-computed embeddings.
        client: Optional BigQuery client.
        embedding_model: Model for generating query embeddings.
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.embeddings_table_id = embeddings_table_id or f"{table_id}_embeddings"
    self._client = client
    self._warned_unlabeled_client = False
    self.embedding_model = embedding_model

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def retrieve_similar_episodes(
      self,
      query: str,
      user_id: str,
      top_k: int = 5,
      since_days: int = 30,
  ) -> list[Episode]:
    """Finds past interactions similar to current query.

    Args:
        query: The current query to find similar episodes for.
        user_id: The user identifier.
        top_k: Number of similar episodes to return.
        since_days: Only consider episodes from the last N days.

    Returns:
        List of Episode objects ranked by similarity.
    """
    since_timestamp = datetime.now(timezone.utc) - timedelta(days=since_days)

    # Try vector search first if embeddings table exists
    try:
      return await self._vector_similarity_search(
          query, user_id, top_k, since_timestamp
      )
    except Exception as e:
      logger.debug(
          "Vector search unavailable, falling back to keyword search: %s", e
      )

    # Fallback to simple keyword matching
    return await self._keyword_search(query, user_id, top_k, since_timestamp)

  async def _vector_similarity_search(
      self,
      query: str,
      user_id: str,
      top_k: int,
      since_timestamp: datetime,
  ) -> list[Episode]:
    """Performs vector similarity search using BigQuery ML."""
    # Generate embedding for query
    query_embedding = await self._generate_embedding(query)
    if query_embedding is None:
      raise ValueError("Could not generate query embedding")

    sql = self._VECTOR_SEARCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        embeddings_table=self.embeddings_table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
            bigquery.ArrayQueryParameter(
                "query_embedding", "FLOAT64", query_embedding
            ),
            bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="memory")

    loop = asyncio.get_event_loop()
    query_job = await loop.run_in_executor(
        None,
        lambda: self.client.query(sql, job_config=job_config),
    )
    results = await loop.run_in_executor(None, lambda: list(query_job.result()))

    episodes = []
    for row in results:
      content = row.get("content")
      if isinstance(content, str):
        try:
          content = json.loads(content)
        except json.JSONDecodeError:
          content = {"text": content}

      user_message = ""
      if isinstance(content, dict):
        user_message = content.get("text_summary") or content.get("text", "")
      else:
        user_message = str(content) if content else ""

      episodes.append(
          Episode(
              session_id=row.get("session_id", ""),
              timestamp=row.get("timestamp", datetime.now(timezone.utc)),
              user_message=user_message,
              agent_response=None,
              similarity_score=1.0 - row.get("distance", 1.0),
          )
      )

    return episodes

  async def _keyword_search(
      self,
      query: str,
      user_id: str,
      top_k: int,
      since_timestamp: datetime,
  ) -> list[Episode]:
    """Performs simple keyword-based search."""
    sql = self._SIMILARITY_SEARCH_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
            bigquery.ScalarQueryParameter(
                "since_timestamp", "TIMESTAMP", since_timestamp
            ),
            bigquery.ScalarQueryParameter("limit", "INT64", top_k * 10),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="memory")

    loop = asyncio.get_event_loop()
    query_job = await loop.run_in_executor(
        None,
        lambda: self.client.query(sql, job_config=job_config),
    )
    results = await loop.run_in_executor(None, lambda: list(query_job.result()))

    # Simple keyword scoring
    query_words = set(query.lower().split())
    scored_episodes: list[tuple[float, Episode]] = []

    for row in results:
      content = row.get("content")
      if isinstance(content, str):
        try:
          content = json.loads(content)
        except json.JSONDecodeError:
          content = {"text": content}

      text = ""
      if isinstance(content, dict):
        text = content.get("text_summary") or content.get("text", "")
      else:
        text = str(content) if content else ""

      text_words = set(text.lower().split())
      if query_words and text_words:
        overlap = len(query_words & text_words)
        score = overlap / len(query_words)
      else:
        score = 0.0

      if score > 0:
        episode = Episode(
            session_id=row.get("session_id", ""),
            timestamp=row.get("timestamp", datetime.now(timezone.utc)),
            user_message=text,
            agent_response=None,
            similarity_score=score,
        )
        scored_episodes.append((score, episode))

    # Sort by score and return top_k
    scored_episodes.sort(key=lambda x: x[0], reverse=True)
    return [ep for _, ep in scored_episodes[:top_k]]

  async def _generate_embedding(self, text: str) -> Optional[list[float]]:
    """Generates embedding for text using configured model."""
    if not self.embedding_model:
      return None

    try:
      from google import genai

      client = genai.Client()
      response = await client.aio.models.embed_content(
          model=self.embedding_model,
          contents=text,
      )
      if response.embeddings:
        return list(response.embeddings[0].values)
    except Exception as e:
      logger.warning("Failed to generate embedding: %s", e)

    return None


class ContextManager:
  """Manages agent context to prevent cognitive overload.

  Implements observation masking and progressive summarization to
  maintain relevant context within token budgets.
  """

  def __init__(
      self,
      max_context_tokens: int = 32000,
      relevance_weight: float = 0.7,
      recency_weight: float = 0.3,
  ) -> None:
    """Initializes ContextManager.

    Args:
        max_context_tokens: Maximum tokens for context window.
        relevance_weight: Weight for relevance in scoring.
        recency_weight: Weight for recency in scoring.
    """
    self.max_tokens = max_context_tokens
    self.relevance_weight = relevance_weight
    self.recency_weight = recency_weight

  def select_relevant_context(
      self,
      current_task: str,
      available_memories: list[Episode],
      current_context_tokens: int = 0,
  ) -> list[Episode]:
    """Selects most relevant memories for current task.

    Implements observation masking to reduce noise and prevent
    cognitive overload from irrelevant context.

    Args:
        current_task: Description of current task.
        available_memories: List of available memory episodes.
        current_context_tokens: Tokens already used in context.

    Returns:
        Selected memories within token budget.
    """
    if not available_memories:
      return []

    # Score memories
    scored_memories: list[tuple[float, Episode]] = []
    now = datetime.now(timezone.utc)

    for memory in available_memories:
      relevance = self._compute_relevance(memory, current_task)
      recency = self._compute_recency_weight(memory.timestamp, now)
      score = relevance * self.relevance_weight + recency * self.recency_weight
      scored_memories.append((score, memory))

    # Sort by score
    scored_memories.sort(key=lambda x: x[0], reverse=True)

    # Select within token budget
    selected: list[Episode] = []
    token_count = current_context_tokens

    for score, memory in scored_memories:
      memory_tokens = self._estimate_tokens(memory)
      if token_count + memory_tokens < self.max_tokens:
        selected.append(memory)
        token_count += memory_tokens

    return selected

  def _compute_relevance(self, memory: Episode, current_task: str) -> float:
    """Computes relevance score between memory and current task."""
    if not memory.user_message or not current_task:
      return 0.0

    task_words = set(current_task.lower().split())
    memory_words = set(memory.user_message.lower().split())

    if not task_words:
      return 0.0

    overlap = len(task_words & memory_words)
    return overlap / len(task_words)

  def _compute_recency_weight(
      self,
      timestamp: Optional[datetime],
      now: datetime,
  ) -> float:
    """Computes recency weight with exponential decay."""
    if timestamp is None:
      return 0.0

    # Ensure timezone aware comparison
    if timestamp.tzinfo is None:
      timestamp = timestamp.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
      now = now.replace(tzinfo=timezone.utc)

    age_hours = (now - timestamp).total_seconds() / 3600
    # Exponential decay with half-life of 24 hours
    return 2 ** (-age_hours / 24)

  def _estimate_tokens(self, memory: Episode) -> int:
    """Estimates token count for a memory."""
    text_length = len(memory.user_message or "")
    if memory.agent_response:
      text_length += len(memory.agent_response)

    # Rough estimate: ~4 characters per token
    return text_length // 4 + 10

  async def summarize_old_context(
      self,
      memories: list[Episode],
      preserve_recent: int = 5,
  ) -> tuple[Optional[str], list[Episode]]:
    """Summarizes older context to save tokens.

    Args:
        memories: List of memory episodes to process.
        preserve_recent: Number of recent memories to keep unsummarized.

    Returns:
        Tuple of (summary string, recent memories to keep).
    """
    if len(memories) <= preserve_recent:
      return None, memories

    old_memories = memories[:-preserve_recent]
    recent_memories = memories[-preserve_recent:]

    # Build text to summarize
    text_parts = []
    for mem in old_memories:
      if mem.user_message:
        text_parts.append(f"User: {mem.user_message}")
      if mem.agent_response:
        text_parts.append(f"Agent: {mem.agent_response}")

    if not text_parts:
      return None, recent_memories

    conversation_text = "\n".join(text_parts)

    try:
      from google import genai
      from google.genai import types

      client = genai.Client()
      response = await client.aio.models.generate_content(
          model="gemini-2.5-flash",
          contents=f"""Summarize the key points from this conversation history,
preserving important facts, user preferences, and decisions made.
Keep the summary concise but informative.

Conversation:
{conversation_text}

Summary:""",
          config=types.GenerateContentConfig(
              temperature=0.3,
              max_output_tokens=500,
          ),
      )

      summary = response.text.strip()
      return summary, recent_memories

    except Exception as e:
      logger.warning("Failed to summarize context: %s", e)
      return None, recent_memories


class UserProfileBuilder:
  """Builds and maintains user profiles from trace data."""

  _USER_STATS_QUERY = """
  SELECT
    COUNT(DISTINCT session_id) as session_count,
    MAX(timestamp) as last_interaction,
    ARRAY_AGG(DISTINCT JSON_EXTRACT_SCALAR(content, '$.tool')) as tools_used
  FROM `{project}.{dataset}.{table}`
  WHERE user_id = @user_id
    AND event_type = 'TOOL_COMPLETED'
  """

  _USER_MESSAGES_QUERY = """
  SELECT
    JSON_EXTRACT_SCALAR(content, '$.text_summary') as message,
    timestamp
  FROM `{project}.{dataset}.{table}`
  WHERE user_id = @user_id
    AND event_type = 'USER_MESSAGE_RECEIVED'
  ORDER BY timestamp DESC
  LIMIT 100
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
  ) -> None:
    """Initializes UserProfileBuilder.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        client: Optional BigQuery client.
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self._client = client
    self._warned_unlabeled_client = False

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def build_profile(self, user_id: str) -> UserProfile:
    """Analyzes all user traces to build a profile.

    Args:
        user_id: The user identifier.

    Returns:
        UserProfile with extracted information.
    """
    profile = UserProfile(user_id=user_id)

    # Get basic stats
    stats_query = self._USER_STATS_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="memory")

    loop = asyncio.get_event_loop()

    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(stats_query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      if results:
        row = results[0]
        profile.session_count = row.get("session_count", 0)
        profile.last_interaction = row.get("last_interaction")
        tools = row.get("tools_used", [])
        profile.preferred_tools = [t for t in tools if t]

    except Exception as e:
      logger.warning("Failed to get user stats: %s", e)

    # Analyze user messages for topics and patterns
    await self._analyze_user_messages(user_id, profile)

    return profile

  async def _analyze_user_messages(
      self,
      user_id: str,
      profile: UserProfile,
  ) -> None:
    """Analyzes user messages to extract topics and patterns."""
    messages_query = self._USER_MESSAGES_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("user_id", "STRING", user_id),
        ]
    )
    job_config = with_sdk_labels(job_config, feature="memory")

    loop = asyncio.get_event_loop()

    try:
      query_job = await loop.run_in_executor(
          None,
          lambda: self.client.query(messages_query, job_config=job_config),
      )
      results = await loop.run_in_executor(
          None, lambda: list(query_job.result())
      )

      messages = [r.get("message", "") for r in results if r.get("message")]

      if messages:
        # Use LLM to extract patterns
        await self._extract_patterns_with_llm(messages, profile)

    except Exception as e:
      logger.warning("Failed to analyze user messages: %s", e)

  async def _extract_patterns_with_llm(
      self,
      messages: list[str],
      profile: UserProfile,
  ) -> None:
    """Uses LLM to extract user patterns from messages."""
    try:
      from google import genai
      from google.genai import types

      messages_text = "\n".join(messages[:50])  # Limit for context

      prompt = f"""Analyze these user messages and extract:
1. Topics of interest (list of topics)
2. Communication style preferences (formal, casual, technical, etc.)
3. Common types of requests/patterns

Messages:
{messages_text}

Output as JSON:
{{"topics": ["topic1", "topic2"], "style": "description", "patterns": ["pattern1", "pattern2"]}}
"""

      client = genai.Client()
      response = await client.aio.models.generate_content(
          model="gemini-2.5-flash",
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.3,
              max_output_tokens=500,
          ),
      )

      response_text = response.text.strip()

      # Extract JSON
      if "{" in response_text:
        start = response_text.index("{")
        end = response_text.rindex("}") + 1
        json_str = response_text[start:end]
        result = json.loads(json_str)

        profile.topics_of_interest = result.get("topics", [])
        profile.communication_style = result.get("style")
        profile.common_requests = result.get("patterns", [])

    except Exception as e:
      logger.warning("Failed to extract patterns with LLM: %s", e)


class BigQueryMemoryService(BaseMemoryService):
  """BigQuery-backed memory service for ADK agents.

  Implements the BaseMemoryService interface using BigQuery traces
  for persistent, searchable agent memory across sessions.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      embedding_model: Optional[str] = None,
  ) -> None:
    """Initializes BigQueryMemoryService.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID.
        table_id: BigQuery table ID.
        client: Optional BigQuery client.
        embedding_model: Model for generating embeddings.
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self._client = client
    self._warned_unlabeled_client = False
    self.embedding_model = embedding_model

    self.session_memory = BigQuerySessionMemory(
        project_id, dataset_id, table_id, client
    )
    self.episodic_memory = BigQueryEpisodicMemory(
        project_id,
        dataset_id,
        table_id,
        client=client,
        embedding_model=embedding_model,
    )
    self.context_manager = ContextManager()
    self.profile_builder = UserProfileBuilder(
        project_id, dataset_id, table_id, client
    )

    # Emit the vanilla-client WARNING exactly once at the service
    # boundary — each child otherwise latches its own warning flag, so a
    # single unlabeled client injected here would produce up to four
    # separate warnings across the service's lifetime. Latch them all
    # up front so only the service-level log fires.
    if isinstance(client, bigquery.Client) and not isinstance(
        client, LabeledBigQueryClient
    ):
      logger.warning(
          "User-provided bigquery.Client is not a "
          "LabeledBigQueryClient; SDK telemetry labels will not be "
          "applied to jobs from this client. To opt in, construct "
          "the client via bigquery_agent_analytics.make_bq_client() "
          "or pass a LabeledBigQueryClient directly."
      )
      self._warned_unlabeled_client = True
      self.session_memory._warned_unlabeled_client = True
      self.episodic_memory._warned_unlabeled_client = True
      self.profile_builder._warned_unlabeled_client = True

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def add_session_to_memory(self, session: Session) -> None:
    """Adds a session to memory.

    Note: With BigQuery analytics plugin, sessions are automatically
    logged. This method is provided for interface compatibility and
    can trigger additional processing like embedding generation.

    Args:
        session: The session to add.
    """
    # Sessions are automatically added by BigQueryAgentAnalyticsPlugin
    # This method can be used to trigger additional processing
    logger.debug(
        "Session %s memory update triggered (auto-logged by plugin)",
        session.id,
    )

  async def search_memory(
      self,
      *,
      app_name: str,
      user_id: str,
      query: str,
  ) -> SearchMemoryResponse:
    """Searches for memories matching the query.

    Args:
        app_name: The application name.
        user_id: The user identifier.
        query: The search query.

    Returns:
        SearchMemoryResponse with matching memories.
    """
    # Retrieve similar episodes
    episodes = await self.episodic_memory.retrieve_similar_episodes(
        query=query,
        user_id=user_id,
        top_k=10,
    )

    # Apply context management
    relevant_episodes = self.context_manager.select_relevant_context(
        current_task=query,
        available_memories=episodes,
    )

    # Convert to MemoryEntry format
    memories = [ep.to_memory_entry() for ep in relevant_episodes]

    return SearchMemoryResponse(memories=memories)

  async def get_user_profile(self, user_id: str) -> UserProfile:
    """Gets the user profile built from trace history.

    Args:
        user_id: The user identifier.

    Returns:
        UserProfile with extracted information.
    """
    return await self.profile_builder.build_profile(user_id)

  async def get_session_context(
      self,
      user_id: str,
      current_session_id: str,
      lookback_sessions: int = 5,
  ) -> list[Episode]:
    """Gets context from recent sessions.

    Args:
        user_id: The user identifier.
        current_session_id: Current session to exclude.
        lookback_sessions: Number of past sessions to consider.

    Returns:
        List of relevant episodes from past sessions.
    """
    return await self.session_memory.get_recent_context(
        user_id=user_id,
        current_session_id=current_session_id,
        lookback_sessions=lookback_sessions,
    )
