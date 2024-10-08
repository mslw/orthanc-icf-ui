import asyncio
from collections import namedtuple
from pathlib import Path
import subprocess
from tempfile import gettempdir
import tomllib
from zipfile import ZipFile

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    Header,
    Footer,
    Input,
    SelectionList,
    Button,
    RichLog,
)
from textual.worker import Worker, WorkerState

import aiofiles
import httpx
import platformdirs


class OrthancClient:
    """Interactions with the Orthanc API"""

    def __init__(self, baseurl):
        self.baseurl = baseurl
        self.client = None

    async def login(self, user, password) -> None:
        """Check if credentials work and remember them

        There is no actual log in action, so we perform a cheap call
        (get system information). If it works, user and pass will be
        used for other calls. Raises an error if it fails.

        """
        if user == "" and password == "":
            self.client = httpx.AsyncClient()
        else:
            self.client = httpx.AsyncClient(auth=(user, password))

        response = await self.client.get(f"{self.baseurl}/system")
        response.raise_for_status()

    async def query(self, date: str) -> list:
        """Perform a date query using /tools/find API

        Runs the /tools/find query for a given StudyDate, and then a
        /studies/{id} query to find matching PatientIDs.

        Returns results as a list of (patientID, study_id) tuples (the
        first coming from dicom, the second being orthanc study ID),
        which can e.g. be plugged directly intu textual's
        SelectionList.

        """
        d = {
            "Level": "Study",
            "Query": {
                "StudyDate": date,
            },
        }

        response = await self.client.post(
            url=f"{self.baseurl}/tools/find",
            json=d,
        )
        response.raise_for_status()
        matched_ids = response.json()

        results = []
        for study_id in matched_ids:
            response = await self.client.get(f"{self.baseurl}/studies/{study_id}")
            d = response.json()
            patientID = d.get("PatientMainDicomTags", {}).get("PatientID")
            results.append((patientID, study_id))  # orthanc study id

        return results

    async def export(self, study_id: str) -> Path:
        """Export dicoms from Orthanc

        Uses the /studies/{id}/archive API endpoint, unpacks and
        removes the zipfile. Returns the path to the extracted
        directory.

        """
        outdir = Path(gettempdir())
        zip_path = outdir.joinpath(f"{study_id}.zip")
        out_path = outdir.joinpath(f"{study_id}")

        async with self.client.stream(
            "GET", f"{self.baseurl}/studies/{study_id}/archive"
        ) as r:
            async with aiofiles.open(zip_path, "wb") as f:
                async for chunk in r.aiter_bytes(chunk_size=10 * 1024):
                    await f.write(chunk)

        with ZipFile(zip_path) as zf:
            zf.extractall(out_path)

        zip_path.unlink()

        return out_path


class OrthancApp(App):
    """Textual app, with UI and logic for the Orthanc-ICF workflow"""

    BINDINGS = [("q", "quit", "Quit")]
    CSS_PATH = "style.tcss"

    def __init__(self):
        super().__init__()
        self.config = self._get_config()
        self.orthanc = OrthancClient(self.config.orthanc_base_url)
        self.listed_studies = {}

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        with Horizontal(classes="onerow"):
            yield Input(placeholder="Username", id="user_input", classes="column")
            yield Input(
                placeholder="Password",
                id="password_input",
                classes="column",
                password=True,
            )
            yield Button("Connect", id="connect_button")

        yield Input(placeholder="Study date", id="date_input", disabled=True)
        yield SelectionList(id="sel_list")
        yield Button("Export", id="export_button", disabled=True)
        yield RichLog(highlight=True, markup=True)
        yield Footer()

    async def call_icf(self, cmd: str, *args) -> None:
        """Helper to call ICF commands as asyncio subprocesses

        Creates the subprocess, awaits its exit, and prints the result
        (ok/error) in the log window.

        """
        log = self.query_one(RichLog)

        # ignore stdin/out for now, just wait for process to exit
        proc = await asyncio.create_subprocess_exec(
            self.config.icf_image,
            cmd,
            *args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        returncode = await proc.wait()

        if returncode == 0:
            msg = "[green]OK[/green]"
        else:
            msg = f"[red]ERROR {returncode}[/red]"

        log.write(f"({msg}) {cmd}")

    async def icf_workflow(self, orthanc_study_ids: list) -> None:
        """Run the workflow: orthanc export, icf utils

        Loops over all selected orthanc studies.

        """
        log = self.query_one(RichLog)

        for s in orthanc_study_ids:
            log.write(f"Processing dicom study id {s}")

            # export dicoms from orthanc
            export_dir = await self.orthanc.export(s)
            log.write(f"Exported {export_dir}")

            # work on subdirectory (<orthanc study id>/<subject ID> <subject name>)
            subdirs = [child for child in export_dir.iterdir() if child.is_dir()]
            assert len(subdirs) == 1
            dicom_dir = subdirs[0]

            # figure out study & visit ID from patient ID
            study_id, visit_id = self._parse_id(self.listed_studies[s])

            result = await self.call_icf(
                "make_studyvisit_archive",
                "--output-dir",  # psychoinformatics-de/inm-icf-utilities/issues/52
                self.config.store_base_dir,
                "--id",
                study_id,
                visit_id,
                dicom_dir,
            )

            result = await self.call_icf(
                "deposit_visit_metadata",
                "--store-dir",
                self.config.store_base_dir,
                "--id",
                study_id,
                visit_id,
            )

            result = await self.call_icf(
                "deposit_visit_dataset",
                "--store-dir",
                self.config.store_base_dir,
                "--id",
                study_id,
                visit_id,
                # todo: --store-url (once we know it)
            )

            result = await self.call_icf(
                "catalogify_studyvisit_from_meta",
                "--store-dir",
                self.config.store_base_dir,
                "--id",
                study_id,
                visit_id,
            )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "date_input":
            sl = self.get_child_by_id("sel_list")
            sl.clear_options()

            if event.input.value != "":
                # orthanc sees "" as "any", but we are different
                self.run_worker(
                    self.orthanc.query(event.input.value),
                    name="query",
                    exclusive=True,
                    exit_on_error=False,
                )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "connect_button":
            user = self.get_widget_by_id("user_input").value
            password = self.get_widget_by_id("password_input").value
            self.run_worker(
                self.orthanc.login(user, password),
                name="login",
                exclusive=True,
                exit_on_error=False,
            )

        if event.button.id == "export_button":
            sl = self.get_child_by_id("sel_list")
            self.run_worker(
                self.icf_workflow(sl.selected), exclusive=True, name="icf_workflow"
            )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        log = self.query_one(RichLog)

        # login to orthanc (credentials check)
        if event.worker.name == "login":
            connect_button = self.get_widget_by_id("connect_button")
            date_input = self.get_widget_by_id("date_input")
            if event.state == WorkerState.SUCCESS:
                log.write("Connected successfully")
                date_input.disabled = False
                connect_button.variant = "success"
            elif event.state == WorkerState.ERROR:
                date_input.disabled = True
                connect_button.variant = "error"

        # query orthanc
        elif event.worker.name == "query":
            if event.state == WorkerState.SUCCESS:
                # add query result to selection list
                sl = self.get_child_by_id("sel_list")
                sl.add_options(event.worker.result)
                # store the mapping for lookup later
                self.listed_studies = {
                    study_id: patientID for patientID, study_id in event.worker.result
                }

        elif event.worker.name == "icf_workflow":
            if event.state == WorkerState.SUCCESS:
                log.write("[green]DONE[/green]")

        # do not let errors pass unnoticed
        if event.state == WorkerState.ERROR:
            log.write(event)
            log.write(event.worker.error)

    def on_selection_list_selected_changed(
        self, event: SelectionList.SelectedChanged
    ) -> None:
        """Enable export button when at least one subject selected"""

        btn = self.get_child_by_id("export_button")
        if len(event.selection_list.selected) > 0:
            btn.disabled = False
        else:
            btn.disabled = True

    def _parse_id(self, identifier) -> tuple[str, str]:
        """Parse dicom PatientID into study and visit identifier

        Here, we rely on convention being used for PatientIDs stored
        in dicom headers, with first underscore delimiting the study
        ID from visit identifier.

        If the split cannot be performed, we make the study ID
        "undefined" and the whole thing becomes a visit ID.

        """

        study, _, visit = identifier.partition("_")
        if visit == "":
            study, visit = "unknown", study

        return study, visit

    def _get_config(self):
        """Read configuration from a file in a standard location"""

        app = "orthanc_textual"
        files = [
            Path(d) / "config.toml"
            for d in (
                platformdirs.user_config_dir(app),
                platformdirs.site_config_dir(app),
            )
        ]

        for config_file in files:
            if config_file.is_file():
                with config_file.open("rb") as f:
                    cfg = tomllib.load(f)
                break
            else:
                msg = f"No config file found. Please create either of {files}."
                raise RuntimeError(msg)

        Config = namedtuple(
            "Config", ["orthanc_base_url", "icf_image", "store_base_dir"]
        )
        config = Config(
            cfg.get("orthanc_base_url"),
            Path(cfg["icf_image"]).expanduser(),
            Path(cfg["store_base_dir"]).expanduser(),
        )
        return config


if __name__ == "__main__":
    app = OrthancApp()
    app.run()
