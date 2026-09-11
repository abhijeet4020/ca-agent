"""Purpose: the operator-facing search interface over the Gold layer. The CLI can already
answer a question, but a chartered accountant should not have to use a terminal to ask one.
Every result carries the client, document and page it came from, because an answer drawn from
a client's own records is only useful if it can be verified against them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ca_agent.config.settings import load_settings  # noqa: E402
from ca_agent.gold.builder import gold_root  # noqa: E402
from ca_agent.gold.embedder import EmbeddingError, SentenceTransformerEmbedder  # noqa: E402
from ca_agent.gold.search import available_indexes, search  # noqa: E402

st.set_page_config(
    page_title="ca-agent search",
    page_icon=":material/search:",
    layout="wide",
)


@st.cache_resource
def _settings():
    """Configuration is read once; vision is disabled because searching spends nothing."""
    return load_settings(overrides={"vision": {"enabled": False}})


@st.cache_resource
def _embedder(model_name: str):
    """The model is a shared resource, loaded once per session rather than per query."""
    return SentenceTransformerEmbedder(model_name)


@st.cache_data(ttl="5m")
def _index_catalogue(scopes_dir_str: str) -> list[dict]:
    """Which clients are searchable, and how much of each is indexed.

    Cached with a short TTL rather than forever: a Gold rebuild publishes new versions while
    the app is open, and a stale catalogue would hide clients that have just become available.
    """
    catalogue = []
    for _, metadata in available_indexes(Path(scopes_dir_str)):
        catalogue.append(
            {
                "scope_id": metadata.scope_id,
                "client": metadata.client,
                "category": metadata.category,
                "chunks": metadata.vector_count,
                "model": metadata.embedding_model,
            }
        )
    return catalogue


settings = _settings()
scopes_dir = gold_root(settings.paths.output_root) / "scopes"

st.title("Search client records")
st.caption(
    "Ask a question in plain language. Every result names the client, document and page it "
    "came from, so it can be checked against the original filing."
)

catalogue = _index_catalogue(str(scopes_dir))

if not catalogue:
    st.warning(
        f"No indexes found at `{scopes_dir}`.",
        icon=":material/warning:",
    )
    st.markdown(
        "Build them first:\n\n"
        "```\n"
        "uv run python src\\main.py run     # raw_data -> silver\n"
        "uv run python src\\main.py gold    # silver -> gold\n"
        "```"
    )
    st.stop()

clients = sorted({entry["client"] for entry in catalogue})
categories = sorted({entry["category"] for entry in catalogue})
total_chunks = sum(entry["chunks"] for entry in catalogue)

with st.sidebar:
    st.subheader("Indexed corpus")
    with st.container(border=True):
        st.metric("Clients", len(catalogue))
        st.metric("Passages", f"{total_chunks:,}")
    st.caption(f"Model: `{catalogue[0]['model']}`")

    st.subheader("Filters")
    chosen_categories = st.multiselect(
        "Category", categories, placeholder="All categories"
    )
    chosen_clients = st.multiselect("Client", clients, placeholder="All clients")
    top_k = st.slider("Results", min_value=3, max_value=25, value=8)
    show_full = st.toggle("Show full passages", value=False)

# The form batches the question so the model is not invoked on every keystroke.
with st.form("search", border=False):
    query = st.text_input(
        "Question",
        placeholder="e.g. depreciation on plant and machinery",
        label_visibility="collapsed",
    )
    submitted = st.form_submit_button("Search", icon=":material/search:", type="primary")

results_slot = st.container()

if submitted and query.strip():
    with results_slot.skeleton():
        try:
            embedder = _embedder(settings.embedding.model_name)
            vector = embedder.encode([query])[0]
            # Filtering happens per selection rather than inside a cached function, so a
            # different filter never re-embeds the same question.
            hits = search(
                scopes_dir,
                vector,
                k=top_k,
                client=None,
                category=None,
            )
        except (EmbeddingError, ValueError) as error:
            results_slot.error(f"Search failed: {error}", icon=":material/error:")
            st.stop()

        if chosen_clients:
            hits = [hit for hit in hits if hit.client in chosen_clients]
        if chosen_categories:
            hits = [hit for hit in hits if hit.category in chosen_categories]

        if not hits:
            results_slot.info(
                "No passages matched. Try broader wording or clear the filters.",
                icon=":material/search_off:",
            )
        else:
            results_slot.caption(f"{len(hits)} passage(s), best match first")
            for hit in hits:
                with results_slot.container(border=True):
                    header = st.container(horizontal=True)
                    header.markdown(f"**{hit.client}**")
                    header.badge(hit.category, color="blue")
                    header.badge(f"{hit.score:.3f}", color="green")

                    passage = hit.chunk.text if show_full else " ".join(
                        hit.chunk.text.split()
                    )[:600]
                    st.markdown(passage)
                    st.caption(
                        f":material/description: {hit.chunk.source_relpath}  \n"
                        f":material/bookmark: {hit.chunk.unit_type}: {hit.chunk.unit_ref}"
                    )
elif submitted:
    results_slot.info("Enter a question to search.", icon=":material/info:")
else:
    with results_slot.container(border=True):
        st.markdown("**Try asking**")
        st.markdown(
            "- depreciation on plant and machinery\n"
            "- GST turnover and input tax credit\n"
            "- bank interest income and TDS deducted\n"
            "- partner remuneration and interest on capital"
        )
