"""PDCA quality-cycle driver — a deterministic state machine over result bundles.

The model: one contribution turns one PDCA cycle (Plan → Do → Check → Act). This
package implements the contribution-body automation — the driver that advances a
bundle through Do and Check's gates+reviewer and STOPS for the human at sign-off.
Plan-authoring, Check sign-off, and Act stay human (docs 01–03).
"""

__version__ = "0.1.0"
