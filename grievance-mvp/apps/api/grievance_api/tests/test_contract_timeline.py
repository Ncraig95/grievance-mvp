import unittest

from grievance_api.services.contract_timeline import (
    calculate_deadline,
    deadline_days_for_contract,
    parse_incident_date,
    resolve_contract_and_incident_date,
)


class ContractTimelineTests(unittest.TestCase):
    def test_deadline_days_map(self) -> None:
        self.assertEqual(deadline_days_for_contract("Core Southest"), 60)
        self.assertEqual(deadline_days_for_contract("core southeast"), 60)
        self.assertEqual(deadline_days_for_contract("wire tech"), 60)
        self.assertEqual(deadline_days_for_contract("mobility"), 45)
        self.assertEqual(deadline_days_for_contract("AT&T Mobility"), 45)
        self.assertEqual(deadline_days_for_contract("att mobility"), 45)
        self.assertEqual(deadline_days_for_contract("City of Jacksonville"), 10)
        self.assertEqual(deadline_days_for_contract("contruction"), 60)
        self.assertEqual(deadline_days_for_contract("construction"), 60)

    def test_parse_incident_date(self) -> None:
        self.assertEqual(str(parse_incident_date("2026-02-20")), "2026-02-20")
        self.assertEqual(str(parse_incident_date("02/20/2026")), "2026-02-20")
        self.assertEqual(str(parse_incident_date("02-20-2026")), "2026-02-20")
        self.assertIsNone(parse_incident_date("20/02/2026"))

    def test_resolve_contract_and_incident(self) -> None:
        contract, incident = resolve_contract_and_incident_date(
            '{"contract":"Mobility","incident_date":"2026-02-20"}'
        )
        self.assertEqual(contract, "Mobility")
        self.assertEqual(str(incident), "2026-02-20")

    def test_calculate_deadline(self) -> None:
        incident = parse_incident_date("2026-02-20")
        self.assertEqual(str(calculate_deadline(incident, 45)), "2026-04-06")


if __name__ == "__main__":
    unittest.main()
