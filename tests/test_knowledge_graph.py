"""Tests for the KnowledgeGraph class."""

import os
import tempfile
from typing import Dict, List
import uuid

import pytest
from donew.see.graph import NER_LABELS, KnowledgeGraph


def test_uni_scrubber():
    """Test Unicode text cleaning."""
    test_cases = [
        (
            "Smart \"quotes\" and 'apostrophes'…",
            "Smart \"quotes\" and 'apostrophes'...",
        ),
        ("Line 1\nLine 2\n  Line 3  ", "Line 1 Line 2 Line 3"),
    ]
    kg = KnowledgeGraph()

    for input_text, expected in test_cases:
        assert kg._uni_scrubber(input_text) == expected


def test_initialization_with_defaults():
    """Test that initialization with defaults succeeds with installed model."""
    kg = KnowledgeGraph()
    assert kg._spacy_model == kg.SPACY_MODEL
    assert kg._gliner_model == kg.GLINER_MODEL

    assert "gliner_spacy" in kg._nlp.pipe_names
    assert "glirel" in kg._nlp.pipe_names


def test_initialization_with_custom_model():
    """Test initialization with custom model name."""
    with pytest.raises(ValueError) as exc_info:
        KnowledgeGraph(spacy_model="invalid_model")
    assert "Could not load spaCy model 'invalid_model'" in str(exc_info.value)


def test_full_kg_pipeline():
    """Test the complete Knowledge Graph pipeline.

    This test verifies:
    1. Text cleaning and preprocessing
    2. Entity extraction
    3. Relationship extraction
    4. Database storage and querying
    """
    # Create a temporary directory for the test database
    with tempfile.TemporaryDirectory() as temp_dir:
        # Initialize KG with database
        kg = KnowledgeGraph(db_path=temp_dir)

        # Test text with known entities and relationships
        text = """
        John Smith, CEO of Tech Corp, announced a partnership with Microsoft.
        The company is headquartered in Silicon Valley.
        Sarah Johnson, CTO of Microsoft, attended the announcement.
        """

        # Analyze the text
        id = uuid.uuid4()
        result = kg.analyze(id, text)

        # Verify entities were extracted
        entities = result["entities"]
        assert len(entities) > 0

        # Check for specific entity types
        entity_labels = {e["label"] for e in entities}
        assert "Person" in entity_labels
        assert "Company" in entity_labels

        # Verify relationships were extracted
        relations = result["relations"]
        assert len(relations) > 0

        # Test database querying
        # Find all companies
        query_result = kg.query(
            """
            MATCH (e:Entity)
            WHERE e.label = 'Company'
            RETURN e.text, e.label
        """
        )
        assert len(query_result) > 0

        # Find relationships between people and companies
        query_result = kg.query(
            """
            MATCH (p:Entity)-[r:Relation]->(o:Entity)
            WHERE p.label = 'Person' AND o.label = 'Company'
            AND r.type = 'FOUNDER'
            RETURN p.text as Founder, o.text as Company
            ORDER BY Founder;
        """
        )
        assert len(query_result) > 0


def test_kuzu_integration():
    """Test KuzuDB integration with entity and relationship extraction.

    This test verifies both in-memory and on-disk database scenarios:
    1. In-memory database (no persistence)
    2. On-disk database with persistence
    3. Entity extraction and storage
    4. Relationship extraction and storage
    5. Complex graph queries
    """
    # Test text with multiple entities and relationships
    text = """
    OpenAI CEO Sam Altman, has partnered with Microsoft.
    The collaboration was announced in San Francisco, where Microsoft's CEO Satya Nadella
    discussed the $10 billion investment. Google's CEO Sundar Pichai responded to the news
    from their headquarters in Mountain View. Meanwhile, Tesla's Elon Musk criticized the deal
    on Twitter, citing concerns about AI safety.
    """

    # Test 1: In-memory database
    id = uuid.uuid4()
    kg_memory = KnowledgeGraph()  # No db_path means in-memory
    result_memory = kg_memory.analyze(id, text)

    print("\nExtracted Entities:")
    for ent in result_memory["entities"]:
        print(f"{ent['text']} ({ent['label']})")

    print("\nExtracted Relations:")
    for rel in result_memory["relations"]:
        print(
            f"{rel['source']['text']} ({rel['source']['label']}) -> {rel['type']} -> {rel['target']['text']} ({rel['target']['label']})"
        )

    print("\nAll Entities in DB:")
    entities = kg_memory.query(
        """
        MATCH (e:Entity)
        RETURN e.text, e.label
        """
    )
    for ent in entities:
        print(f"{ent['e.text']} ({ent['e.label']})")

    print("\nAll Relations in DB:")
    all_rels = kg_memory.query(
        """
        MATCH (p:Entity)-[r:Relation]->(o:Entity)
        RETURN p.text as Source, p.label as SrcLabel, 
               r.type as Type, 
               o.text as Target, o.label as TgtLabel
        """
    )
    for rel in all_rels:
        print(
            f"{rel['Source']} ({rel['SrcLabel']}) -> {rel['Type']} -> {rel['Target']} ({rel['TgtLabel']})"
        )

    # Now try the specific founder query
    print("\nFounder Relations:")
    founder_rels = kg_memory.query(
        """
        MATCH (p:Entity)-[r:Relation]->(o:Entity)
        WHERE p.label = 'Person' AND o.label = 'Company'
        AND r.type = 'FOUNDER'
        RETURN p.text as Founder, o.text as Company
        ORDER BY Founder;
        """
    )
    for rel in founder_rels:
        print(f"{rel['Founder']} -> FOUNDER -> {rel['Company']}")

    # Test 2: On-disk database
    with tempfile.TemporaryDirectory() as temp_dir:
        kg_disk = KnowledgeGraph(db_path=temp_dir)
        result_disk = kg_disk.analyze(id, text)

        # Verify entities were extracted
        entities = result_disk["entities"]
        assert len(entities) > 0

        # Check for specific entities
        entity_texts = {e["text"] for e in entities}
        expected_entities = {
            "OpenAI",
            "Sam Altman",
            "Microsoft",
            "Google",
            "Sundar Pichai",
            "Elon Musk",
            "Twitter",
        }
        # We should find all of these entities
        assert len(entity_texts.intersection(expected_entities)) == len(
            expected_entities
        )

        # Check entity types
        entity_labels = {e["label"] for e in entities}
        assert "Person" in entity_labels
        assert "Company" in entity_labels
        assert "City" in entity_labels

        # Verify relationships were extracted
        relations = result_disk["relations"]
        assert len(relations) > 0

        # Test complex graph queries
        # Find all CEOs and their companies
        ceo_query = kg_disk.query(
            """
            MATCH (p:Entity)-[r:Relation]->(o:Entity)
            WHERE p.label = 'Person' AND o.label = 'Company'
            AND r.type = 'FOUNDER'
            RETURN p.text as Founder, o.text as Company
            ORDER BY Founder;
        """
        )
        assert len(ceo_query) > 0
