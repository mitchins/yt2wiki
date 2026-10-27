"""PR3 (Sonar S3776 backend/core refactors): refactor-sensitive invariants.

Existing suites cover the refactored functions end-to-end (test_asr_http,
test_remote_provider, test_generations, test_verification, test_basics,
test_web_product); these tests pin the invariants most at risk from the
restructuring: literal-scanner precedence/ordering and the
CUDA_VISIBLE_DEVICES device mapping. No models, no network, no GPU.
"""

from __future__ import annotations

from yt2wiki.literals import extract_literals


def _kinds(values):
    return [(lit.kind, lit.literal) for lit in values]


def test_high_confidence_identifiers_claim_spans_before_loose_patterns():
    """A dotted quad is never also reported as a version; order is stable."""
    text = "Set the server to 192.168.1.10 in config v2.1.0 and open port 8080."
    out = extract_literals(text)
    kinds = _kinds(out)
    assert ("ip_address", "192.168.1.10") in kinds
    assert ("version", "v2.1.0") in kinds
    assert ("port", "port 8080") in kinds
    # the IP must not double-report as anything else
    assert [k for k, _ in kinds if "192.168.1.10" in _] == ["ip_address"]


def test_path_value_trailing_punctuation_stripped_and_flag_values_kept():
    text = "Edit /etc/fstab. Then run `sudo mount -a` with flag --force now."
    out = extract_literals(text)
    kinds = _kinds(out)
    assert ("path", "/etc/fstab") in kinds
    assert any(k == "flag" and "--force" in v for k, v in kinds)


def test_command_extraction_from_cue_and_prompt_lines():
    text = "You can run the command sudo dd if=x.img of=/dev/sda. $ systemctl restart nginx"
    out = extract_literals(text)
    commands = [lit.literal for lit in out if lit.kind == "command"]
    assert any("sudo dd" in c for c in commands)
    assert any("systemctl restart nginx" in c for c in commands)


def test_empty_input_yields_no_literals():
    assert extract_literals("") == []


def test_visible_device_mapping(monkeypatch):
    from yt2wiki.gpu import GpuSample, _visible_device_ordinal

    gpus = [
        GpuSample(free_gb=3, total_gb=24, used_gb=21, index=0, name="a"),
        GpuSample(free_gb=20, total_gb=24, used_gb=4, index=1, name="b"),
        GpuSample(free_gb=10, total_gb=24, used_gb=14, index=2, name="c"),
    ]

    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    assert _visible_device_ordinal(gpus) is None

    # visible ordinal of the freest *visible* GPU (PCI 2, 10GB free; PCI 1 hidden)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,2")
    assert _visible_device_ordinal(gpus) == 1

    # unparsable variable falls through
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "x,y")
    assert _visible_device_ordinal(gpus) is None

    # variable matching no probed GPU falls through
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "7")
    assert _visible_device_ordinal(gpus) is None
