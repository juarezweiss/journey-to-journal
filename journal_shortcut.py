"""Build content-free native Shortcuts; signed files contain no journal data."""
import plistlib
import uuid
from pathlib import Path

IMPORT_NAME = "Journey Import Entry"
PREFLIGHT_NAME = "Journey Import Preflight"


def attachment(value):
    return {"Value": value, "WFSerializationType": "WFTextTokenAttachment"}


def token_string(parts):
    text, refs = "", {}
    for part in parts:
        if isinstance(part, dict):
            refs["{%d, 1}" % len(text)] = part["Value"]
            text += "\ufffc"
        else:
            text += part
    return {"Value": {"string": text, "attachmentsByRange": refs},
            "WFSerializationType": "WFTextTokenString"}


def workflow(preflight=False):
    actions = []

    def add(identifier, **parameters):
        key = str(uuid.uuid4()).upper()
        actions.append({"WFWorkflowActionIdentifier": identifier,
                        "WFWorkflowActionParameters": {"UUID": key, **parameters}})
        return attachment({"Type": "ActionOutput", "OutputUUID": key, "OutputName": "Result"})

    incoming = attachment({"Type": "ExtensionInput"})
    first = add("is.workflow.actions.getitemfromlist", WFInput=incoming, WFItemSpecifier="First Item")
    text = add("is.workflow.actions.detect.text", WFInput=first)
    payload = add("is.workflow.actions.detect.dictionary", WFInput=text)

    def field(name):
        return add("is.workflow.actions.getvalueforkey", WFInput=payload, WFDictionaryKey=name)

    html = field("html")
    rich = add("is.workflow.actions.getrichtextfromhtml", WFHTML=html)
    # Avoid Shortcuts' string parser, which discards fractional seconds.
    date = add("is.workflow.actions.properties.files", WFInput=first,
               WFContentItemPropertyName="Last Modified Date")
    message = field("text")
    extensions = ("jpg", "jpeg", "png", "heic", "heif", "gif", "tif", "tiff", "mp4", "mov", "m4v")
    media = add("is.workflow.actions.filter.files", WFContentItemInputParameter=incoming,
                WFContentItemLimitEnabled=False,
                WFContentItemFilter={"WFSerializationType": "WFContentPredicateTableTemplate", "Value": {
                    "WFActionParameterFilterPrefix": 0,  # match any extension
                    "WFContentPredicateBoundedDate": False,
                    "WFActionParameterFilterTemplates": [
                        {"Operator": 4, "Property": "File Extension", "Removable": True,
                         "Values": {"String": extension, "Unit": 4}} for extension in extensions]}})
    if preflight:
        rendered = add("is.workflow.actions.gethtmlfromrichtext", WFInput=rich, WFMakeFullDocument=True)
        formatted = add("is.workflow.actions.format.date", WFDate=token_string([date]), WFDateFormatStyle="Custom",
                        WFDateFormat="yyyy-MM-dd'T'HH:mm:ss.SSSXXXXX")
        count = add("is.workflow.actions.count", WFInput=media, Input=media, WFCountType="Items")
        add("is.workflow.actions.gettext", WFTextActionText=token_string([
            formatted, "\n---MEDIA---\n", count, "\n---HTML---\n", rendered]))
    else:
        add("com.apple.journal.CreateEntryIntent",
            AppIntentDescriptor={"AppIntentIdentifier": "CreateEntryIntent", "BundleIdentifier": "com.apple.journal",
                                 "Name": "Journal", "TeamIdentifier": "0000000000"},
            OpenWhenRun=True, message=token_string([message]), title="",
            entryDate=token_string([date]), mediaItems=media, entryBookmark=False)
        # Only a fixed receipt goes to stdout/output; never the entry body.
        add("is.workflow.actions.gettext", WFTextActionText="journey-import-ok")
    return {"WFWorkflowName": PREFLIGHT_NAME if preflight else IMPORT_NAME,
            "WFWorkflowActions": actions, "WFWorkflowTypes": [],
            "WFWorkflowInputContentItemClasses": ["WFGenericFileContentItem", "WFImageContentItem"],
            "WFWorkflowOutputContentItemClasses": [], "WFWorkflowHasOutputFallback": False,
            "WFWorkflowImportQuestions": [], "WFQuickActionSurfaces": [],
            "WFWorkflowClientRelease": "26.0", "WFWorkflowClientVersion": "4000.0",
            "WFWorkflowMinimumClientVersion": 900, "WFWorkflowMinimumClientVersionString": "900",
            "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 59511, "WFWorkflowIconStartColor": 4282601983}}


def build(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for preflight in (False, True):
        name = PREFLIGHT_NAME if preflight else IMPORT_NAME
        path = directory / (name + ".unsigned.shortcut")
        path.write_bytes(plistlib.dumps(workflow(preflight)))
    setup = workflow(False)
    setup["WFWorkflowActions"] = [action for action in setup["WFWorkflowActions"]
                                  if action["WFWorkflowActionIdentifier"] != "com.apple.journal.CreateEntryIntent"]
    (directory / (IMPORT_NAME + ".setup.unsigned.shortcut")).write_bytes(plistlib.dumps(setup))
    print("Built two content-free shortcuts. Sign and add them to Shortcuts; see IMPORT.md.")


if __name__ == "__main__":
    build(Path(__file__).with_name("shortcuts"))
