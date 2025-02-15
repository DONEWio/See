"""Utility functions for DoNew."""

import asyncio
from typing import Any, Optional
from pydantic import BaseModel


def run_sync(coro: Any, error_message: Optional[str] = None) -> Any:
    """Run an async operation synchronously.

    This provides sync interface to async methods.

    Args:
        coro: The coroutine to run
        error_message: Optional custom error message for async context detection

    Returns:
        The result of the coroutine

    Raises:
        RuntimeError: If called from an async context
    """
    if asyncio.events._get_running_loop() is not None:
        raise RuntimeError(
            error_message
            or "Cannot use sync API inside an async context. Use async methods instead."
        )

    try:
        # Try to get the current event loop
        loop = asyncio.get_event_loop()
    except RuntimeError:
        # If no event loop exists, create a new one
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        should_close = True
    else:
        should_close = False

    try:
        return loop.run_until_complete(coro)
    finally:
        if should_close:
            loop.close()
            asyncio.set_event_loop(None)


def spacy_model_with_opinionated_default() -> str:
    import spacy

    models = [
        "en_core_web_md",
        "en_core_web_sm",
        "en_core_web_lg",
    ] + spacy.util.get_installed_models()
    for model in models:
        if spacy.util.is_package(model):
            return model
    raise ValueError("No installed spaCy model found")


def enable_tracing():
    import subprocess
    import sys
    import os
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from openinference.instrumentation.smolagents import SmolagentsInstrumentor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.trace.export import (
        ConsoleSpanExporter,
        SimpleSpanProcessor,
    )

    # Check if tracing is already enabled
    if hasattr(enable_tracing, "_tracing_enabled"):
        print("🔍 Tracing is already enabled at http://localhost:6006/projects")
        return

    print("🚀 Enabling tracing...")

    # Start phoenix server in background if not already running
    try:
        process = subprocess.Popen(
            ["phoenix", "serve"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        import select
        # Wait up to 0.5 seconds for any output from stdout and print it if available
        rlist, _, _ = select.select([process.stdout], [], [], 3)
        if rlist:
            output_line = process.stdout.readline()
            print(output_line, end='')
        # Store the process for later cleanup
        enable_tracing._server_process = process
        print("📡 Started Phoenix server")
    except Exception as e:
        print(f"⚠️  Warning: Could not start phoenix server: {e}")

    endpoint = "http://0.0.0.0:6006/v1/traces"
    trace_provider = TracerProvider()
    trace_provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint)))
    
    # Set as global provider
    trace.set_tracer_provider(trace_provider)
    
    # Instrument after setting provider
    SmolagentsInstrumentor().instrument()

    # Mark tracing as enabled
    enable_tracing._tracing_enabled = True
    print("✅ Tracing enabled - View traces at http://0.0.0.0:6006/projects")


def disable_tracing():
    """Disable tracing and cleanup resources."""
    if not hasattr(enable_tracing, "_tracing_enabled"):
        print("ℹ️  Tracing is not enabled")
        return

    print("🛑 Disabling tracing...")

    if hasattr(enable_tracing, "_server_process"):
        try:
            enable_tracing._server_process.terminate()
            enable_tracing._server_process.wait(
                timeout=5
            )  # Wait up to 5 seconds for graceful shutdown
            print("📡 Stopped Phoenix server")
        except Exception as e:
            print(f"⚠️  Warning: Error while stopping phoenix server: {e}")
            try:
                enable_tracing._server_process.kill()  # Force kill if terminate doesn't work
                print("📡 Force killed Phoenix server")
            except:
                pass

        delattr(enable_tracing, "_server_process")

    if hasattr(enable_tracing, "_tracing_enabled"):
        delattr(enable_tracing, "_tracing_enabled")

    print("✅ Tracing disabled")

def is_pydantic_model(schema):
    return isinstance(schema, BaseModel) or (isinstance(schema, type) and issubclass(schema, BaseModel))

def parse_to_pydantic(data, schema):
    if is_pydantic_model(schema):
        return schema.model_validate(data)
    return data




def pydantic_model_to_simple_schema(
        model_or_schema: BaseModel | dict[str, Any],
        _processed_refs: set[str] = {}, # Circular Reference Protection
    ) -> dict:
    """
    This transforms a pydantic model or json schema into
    a simple schema that is easier understood by even smaller LLMs
    TODO: Handle Union types with sub-schemas?
    """
    def transform_property(prop_name: str, prop_info: dict) -> str:
        if "anyOf" in prop_info or "oneOf" in prop_info:
            types = [t.get("type", "string") for t in (prop_info.get("anyOf") or prop_info.get("oneOf")) if t.get("type")]
            prop_info["type"] = '|'.join(types)
        if prop_info.get("$ref"):
            ref_name = prop_info.get("$ref").split("/")[-1]
            if not ref_name in _processed_refs:
                _processed_refs[ref_name] = pydantic_model_to_simple_schema(schema.get("$defs", {}).get(ref_name, {}))
            return _processed_refs[ref_name]
        if prop_info.get("type") == "array" and prop_info.get("items",{}).get("$ref"):
            ref_name = prop_info.get("items",{}).get("$ref").split("/")[-1]
            if not ref_name in _processed_refs:
                _processed_refs[ref_name] = pydantic_model_to_simple_schema(schema.get("$defs", {}).get(ref_name, {}))
            return [pydantic_model_to_simple_schema(schema.get("$defs", {}).get(ref_name, {}))]
        description = prop_info.get("description", prop_info.get("title", prop_name))
        item_type = f"array[{prop_info.get('items',{}).get('type', 'string')}]" if prop_info.get("type") == "array" else prop_info.get("type", "string")
        choices = f"[CHOICES: {'|'.join([str(choice) for choice in prop_info.get('enum',[])])}]" if "enum" in prop_info else ''
        required = "[REQUIRED]" if prop_info.get("required", False) else ''
        default = f"[DEFAULT: {prop_info.get('default', '')}]" if "default" in prop_info else ''
        return f"<{item_type}>{' '+description if description else ''}{' '+choices if choices else ''}{' '+required if required else ''}{' '+default if default else ''}"
    try:
        schema = model_or_schema if isinstance(model_or_schema, dict) else model_or_schema.model_json_schema()
        properties = schema["properties"]
        
        result = {prop_name: transform_property(prop_name, prop_info) for prop_name, prop_info in properties.items()}
        if "description" in schema:
            result["__description__"] = schema["description"]
        return result
    except Exception as e:
        raise ValueError(f"Error processing schema: {str(e)}")
    



