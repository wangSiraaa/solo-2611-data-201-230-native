"""Core ingest → assemble → version pipeline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .content import DETAIL_COLUMNS, digest, parse_records, perform_etl, render_csv, Record
from .errors import BuildError, Rejected
from .events import Event, previous_day
from .store import PathResolver, StateStore, write_json

SOURCE_ORDER = ("sales", "fx")


def required_partition(source: str, target_date: str) -> str:
    """The partition a target date needs from one source.

    sales on day D needs same-day sales; fx on D needs the previous calendar
    day's rates.
    """
    return target_date if source == "sales" else previous_day(target_date)


def vector_key(vector: dict[str, Any]) -> tuple:
    parts = []
    for source in SOURCE_ORDER:
        entry = vector.get(source)
        parts.append(
            (source, None)
            if entry is None
            else (source, entry["partition"], entry["version"], entry["sha256"])
        )
    return tuple(parts)


def empty_output_state() -> dict[str, Any]:
    return {
        "generation": 0,
        "current": None,
        "last_failure": None,
        "last_rejection": None,
        "history": [],
    }


class Pipeline:
    def __init__(self, config: dict[str, Any], out_dir: Path) -> None:
        self.target_dates: list[str] = list(config["target_dates"])
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.paths = PathResolver(Path(config["input_root"]))
        self.store = StateStore(out_dir)
        self.state = self.store.data

    # ---------------------------------------------------------------- ingest
    def ingest(self, items: Iterable[tuple[str, Any]]) -> dict[str, Any]:
        run = self.store.begin_run("ingest-events")
        results: list[dict[str, Any]] = []
        for kind, payload in items:
            seq = self.state["event_seq"] + 1
            self.state["event_seq"] = seq
            if kind == "rejected":
                raw_event = payload.pop("event", None)
                self.store.record_event(
                    seq, raw_event, "rejected", payload["code"], reason=payload.get("reason")
                )
                results.append(
                    {
                        "index": payload.get("index"),
                        "seq": seq,
                        "outcome": "rejected",
                        "code": payload["code"],
                        "reason": payload.get("reason"),
                    }
                )
                continue
            results.append(self._process_event(payload, run, seq))
        self.store.finish_run(run)
        self.store.save()
        return {
            "run_id": run["run_id"],
            "status": run["status"],
            "processed": len(results),
            "results": results,
            "outputs": run["outputs"],
            "created": run["created"],
            "reused": run["reused"],
        }

    def _process_event(self, event: Event, run: dict[str, Any], seq: int) -> dict[str, Any]:
        try:
            result = self._accept_event(event, run, seq)
        except Rejected as exc:
            self.store.record_event(
                seq, event.as_dict(), "rejected", exc.code, reason=exc.message
            )
            self._record_rejection_for_targets(event, seq, exc)
            return {
                "index": event.index,
                "seq": seq,
                "event_id": event.event_id,
                "outcome": "rejected",
                "code": exc.code,
                "reason": exc.message,
            }

        self.store.record_event(
            seq,
            event.as_dict(),
            result["outcome"],
            result["code"],
            accepted_seq=seq if result["outcome"] == "accepted" else None,
        )
        return {
            "index": event.index,
            "seq": seq,
            "event_id": event.event_id,
            "outcome": result["outcome"],
            "code": result["code"],
            "effective_changed": result["effective_changed"],
            "affected_targets": result["affected_targets"],
        }

    def _accept_event(self, event: Event, run: dict[str, Any], seq: int) -> dict[str, Any]:
        # 1. Resolve the path without leaving input_root.
        path = self.paths.resolve(event.path)

        # 2. The notified file must exist and match the notified digest.
        try:
            data = path.read_bytes()
        except OSError:
            raise Rejected("path_missing", "referenced file cannot be read", path=event.path) from None
        actual = digest(data)
        if actual != event.sha256:
            raise Rejected(
                "sha256_mismatch",
                "file digest does not match event",
                path=event.path,
                expected=event.sha256,
                actual=actual,
            )

        # 3. Re-verify structure, primary keys and fields ourselves.
        records = parse_records(event.source, data)

        # 4. One event_id may not describe two different contents.
        prior = self.store.event_ids.get(event.event_id)
        if prior is not None and prior["fingerprint"] != event.fingerprint():
            raise Rejected(
                "event_id_conflict",
                "event_id was already completed with different content",
                event_id=event.event_id,
                prior=prior["fingerprint"],
                received=event.fingerprint(),
            )

        # 5. One partition version may not be reused for different content.
        day_state = self.store.partitions.setdefault(event.source, {}).setdefault(
            event.partition, {"versions": {}, "effective": None}
        )
        stored = day_state["versions"].get(str(event.version))
        if stored is not None:
            if stored["sha256"] != event.sha256:
                raise Rejected(
                    "version_content_conflict",
                    "partition version was already used with different content",
                    source=event.source,
                    partition=event.partition,
                    version=event.version,
                    prior_sha256=stored["sha256"],
                    received_sha256=event.sha256,
                )
            if event.event_id not in stored["event_ids"]:
                stored["event_ids"].append(event.event_id)
            self.store.event_ids.setdefault(
                event.event_id,
                {"fingerprint": event.fingerprint(), "path": event.path, "seq": seq},
            )
            return {
                "outcome": "duplicate",
                "code": "duplicate_partition_version",
                "effective_changed": False,
                "affected_targets": [],
            }

        # 6. Persist the verified bytes as an immutable snapshot.
        snapshot_rel = (
            Path("content")
            / event.source
            / event.partition
            / f"v{event.version}-{event.sha256[:16]}.csv"
        )
        snapshot_path = self.out_dir / snapshot_rel
        if not snapshot_path.exists():
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_bytes(data)

        previous = day_state["effective"]
        day_state["versions"][str(event.version)] = {
            "version": event.version,
            "sha256": event.sha256,
            "path": event.path,
            "snapshot": str(snapshot_rel),
            "event_ids": [event.event_id],
            "received_seq": seq,
            "record_count": len(records),
        }
        self.store.event_ids[event.event_id] = {
            "fingerprint": event.fingerprint(),
            "path": event.path,
            "seq": seq,
        }

        # 7. Effective version is the highest received + verified version.
        effective_changed = previous is None or event.version > previous["version"]
        if effective_changed:
            day_state["effective"] = self._effective_entry(event.partition, day_state)

        affected = [
            target
            for target in self.target_dates
            if required_partition(event.source, target) == event.partition
        ]
        if not affected:
            outcome, code = "accepted", "accepted_no_target"
        elif not effective_changed:
            outcome, code = "accepted", "accepted_not_effective"
        else:
            outcome, code = "accepted", "accepted_effective"

        for target in affected:
            state = self.store.outputs.setdefault(target, empty_output_state())
            if effective_changed:
                # Only outputs whose effective input vector actually moved.
                self._assemble(target, state, run, seq, event)
            else:
                self._note_late_version(target, state, event, seq)

        return {
            "outcome": outcome,
            "code": code,
            "effective_changed": effective_changed,
            "affected_targets": affected,
        }

    @staticmethod
    def _effective_entry(partition: str, day_state: dict[str, Any]) -> dict[str, Any]:
        best = max(day_state["versions"].values(), key=lambda item: item["version"])
        return {
            "partition": partition,
            "version": best["version"],
            "sha256": best["sha256"],
            "snapshot": best["snapshot"],
        }

    def _note_late_version(
        self, target: str, state: dict[str, Any], event: Event, seq: int
    ) -> None:
        state["last_late_version"] = {
            "event_seq": seq,
            "event_id": event.event_id,
            "source": event.source,
            "partition": event.partition,
            "version": event.version,
            "message": "late older version accepted but did not change the effective vector",
        }

    # ------------------------------------------------------------- assembling
    def _latest_vector(self, target: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        vector: dict[str, Any] = {}
        missing: list[dict[str, Any]] = []
        for source in SOURCE_ORDER:
            day = required_partition(source, target)
            entry = self.store.effective(source, day)
            if entry is None:
                vector[source] = None
                missing.append({"source": source, "partition": day})
            else:
                vector[source] = {
                    "partition": entry["partition"],
                    "version": entry["version"],
                    "sha256": entry["sha256"],
                }
        return vector, missing

    def _assemble(
        self,
        target: str,
        state: dict[str, Any],
        run: dict[str, Any],
        seq: int,
        event: Event,
    ) -> None:
        vector, missing = self._latest_vector(target)
        if missing:
            # Dependencies must be complete before the ETL is invoked at all.
            state["last_failure"] = {
                "run_id": run["run_id"],
                "event_seq": seq,
                "event_id": event.event_id,
                "input_vector": vector,
                "code": "missing_dependencies",
                "message": "required partitions have not been received and verified",
                "missing": missing,
                "etl_invoked": False,
            }
            run["outputs"][target] = {
                "ok": False,
                "input_vector": vector,
                "code": "missing_dependencies",
                "etl_invoked": False,
            }
            return

        current = state.get("current")
        if current is not None and vector_key(current["input_vector"]) == vector_key(vector):
            # Same effective vector: reuse bytes and keep the generation.
            run["reused"][target] = {
                "input_vector": vector,
                "generation": current["generation"],
            }
            return

        run["etl"].append(
            {
                "target": target,
                "event_seq": seq,
                "event_id": event.event_id,
                "input_vector": vector,
                "status": "started",
            }
        )
        etl_record = run["etl"][-1]
        try:
            records = self._load_vector_records(vector)
            detail, unresolved = perform_etl(records["sales"], records["fx"])
            if unresolved:
                referenced = "fx " + ", ".join(
                    f"{item['order_id']}={item['currency']}" for item in unresolved
                )
                raise BuildError(
                    "missing_rate",
                    f"no rate for one or more sales currencies ({referenced})",
                    unresolved=unresolved,
                )
        except (BuildError, Rejected) as exc:
            etl_record.update(status="failed", code=exc.code)
            failure = {
                "run_id": run["run_id"],
                "event_seq": seq,
                "event_id": event.event_id,
                "input_vector": vector,
                "code": exc.code,
                "message": exc.message,
                "etl_invoked": True,
            }
            if isinstance(exc, BuildError):
                failure.update(exc.detail)
            state["last_failure"] = failure
            run["outputs"][target] = {
                "ok": False,
                "input_vector": vector,
                "code": exc.code,
                "etl_invoked": True,
            }
            return

        generation = state["generation"] + 1
        state["generation"] = generation
        rel_path = Path("assemblies") / target / f"v{generation}.csv"
        body = render_csv(
            DETAIL_COLUMNS,
            ([row[column] for column in DETAIL_COLUMNS] for row in detail),
        )
        abs_path = self.out_dir / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(body)
        (abs_path.parent / "current.csv").write_bytes(body)
        output_sha = digest(body)

        binding = {
            "target_date": target,
            "generation": generation,
            "created_run": run["run_id"],
            "created_event_seq": seq,
            "created_event_id": event.event_id,
            "input_vector": {
                source: {
                    **vector[source],
                    "snapshot": (self.store.effective(source, vector[source]["partition"]) or {}).get(
                        "snapshot"
                    ),
                }
                for source in SOURCE_ORDER
            },
            "output": {
                "path": str(rel_path),
                "sha256": output_sha,
                "row_count": len(detail),
                "current_path": str(Path("assemblies") / target / "current.csv"),
            },
        }
        write_json(abs_path.parent / "current.json", binding)
        state["current"] = binding
        state["last_failure"] = None
        state["history"].append(
            {
                "generation": generation,
                "run_id": run["run_id"],
                "event_seq": seq,
                "input_vector": vector,
                "output_sha256": output_sha,
            }
        )
        etl_record.update(status="success", generation=generation, output_sha256=output_sha)
        run["outputs"][target] = {
            "ok": True,
            "input_vector": vector,
            "generation": generation,
            "output_sha256": output_sha,
            "etl_invoked": True,
        }
        run["created"][target] = generation

    def _load_vector_records(self, vector: dict[str, Any]) -> dict[str, list[Record]]:
        loaded: dict[str, list[Record]] = {}
        for source in SOURCE_ORDER:
            entry = vector[source]
            day_state = self.store.partitions[source][entry["partition"]]
            stored = day_state["versions"][str(entry["version"])]
            snapshot = self.out_dir / stored["snapshot"]
            try:
                data = snapshot.read_bytes()
            except OSError:
                raise BuildError(
                    "snapshot_missing",
                    "verified snapshot is unavailable",
                    path=stored["snapshot"],
                ) from None
            if digest(data) != entry["sha256"]:
                raise BuildError(
                    "snapshot_changed",
                    "verified snapshot digest changed",
                    path=stored["snapshot"],
                )
            loaded[source] = parse_records(source, data)
        return loaded

    def _record_rejection_for_targets(self, event: Event, seq: int, exc: Rejected) -> None:
        day_rejection = {
            "event_seq": seq,
            "event_id": event.event_id,
            "code": exc.code,
            "message": exc.message,
            "source": event.source,
            "partition": event.partition,
            "version": event.version,
            "sha256": event.sha256,
        }
        # Keep the highest rejected version ever seen for the partition so a
        # later lower-version rejection cannot hide a rejected newer notice.
        source_rejections = self.store.rejections.setdefault(event.source, {})
        previous = source_rejections.get(event.partition)
        if previous is None or event.version >= previous["version"]:
            source_rejections[event.partition] = day_rejection
        for target in self.target_dates:
            if required_partition(event.source, target) != event.partition:
                continue
            state = self.store.outputs.setdefault(target, empty_output_state())
            state["last_rejection"] = day_rejection

    # ----------------------------------------------------------------- status
    def _unresolved_rejection(self, target: str) -> dict[str, Any] | None:
        """Return a rejection that invalidates the latest-effective vector.

        Only rejections for a version higher than the effective one count: a
        rejected old completion is not "newer data", while a rejected v3 for a
        partition where v2 is effective means the latest notice for that input
        failed and the old generation is stale.
        """
        for source in SOURCE_ORDER:
            day = required_partition(source, target)
            rejection = self.store.last_rejection(source, day)
            if rejection is None:
                continue
            effective = self.store.effective(source, day)
            effective_version = effective["version"] if effective else 0
            if rejection["version"] > effective_version:
                return rejection
        return None

    def status(self) -> dict[str, Any]:
        targets: list[dict[str, Any]] = []
        for target in self.target_dates:
            vector, missing = self._latest_vector(target)
            state = self.store.outputs.get(target, empty_output_state())
            current = state.get("current")
            up_to_date = (
                current is not None
                and not missing
                and vector_key(current["input_vector"]) == vector_key(vector)
            )

            failure_reason: dict[str, Any] | None = None
            stale_rejection = self._unresolved_rejection(target)
            if not up_to_date:
                if missing:
                    failure_reason = {
                        "code": "missing_dependencies",
                        "message": "required partitions have not been received and verified",
                        "missing": missing,
                    }
                elif state.get("last_failure") and vector_key(
                    state["last_failure"]["input_vector"]
                ) == vector_key(vector):
                    failure = state["last_failure"]
                    failure_reason = {
                        key: failure[key]
                        for key in ("code", "message", "unresolved", "missing")
                        if key in failure
                    }
                    failure_reason["event_seq"] = failure["event_seq"]
                else:
                    failure_reason = {
                        "code": "latest_inputs_not_processed",
                        "message": "latest available input vector has not produced a successful output",
                    }
            elif stale_rejection is not None:
                # A newer completion notice for one dependency was rejected, so
                # the old output must not be presented as reflecting latest data.
                failure_reason = {
                    "code": "latest_completion_rejected",
                    "message": "a newer completion notice for a dependency was rejected",
                    "rejected": stale_rejection,
                }
                up_to_date = False

            recent_success = None
            if current is not None:
                recent_success = {
                    "generation": current["generation"],
                    "output_path": current["output"]["path"],
                    "output_sha256": current["output"]["sha256"],
                    "row_count": current["output"]["row_count"],
                }

            targets.append(
                {
                    "date": target,
                    "required": {
                        source: required_partition(source, target) for source in SOURCE_ORDER
                    },
                    "latest_available_vector": vector,
                    "missing_dependencies": missing,
                    "recent_success_vector": current["input_vector"] if current else None,
                    "recent_success": recent_success,
                    "latest_inputs_processed": up_to_date,
                    "failure_reason": failure_reason,
                    "last_failure": state.get("last_failure"),
                    "last_rejection": state.get("last_rejection"),
                    "generation": state["generation"],
                }
            )
        return {
            "targets": targets,
            "runs": self.state["runs"],
            "events": self.state["events"],
        }
