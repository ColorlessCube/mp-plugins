# Official MoviePilot fixture provenance and license

These test-only sources are copied byte-for-byte from jxxghp/MoviePilot,
revision `4ed5fa079897e4b02615abb01b78efc1d1272cae` (tag `v3.2.5`),
https://github.com/jxxghp/MoviePilot/tree/4ed5fa079897e4b02615abb01b78efc1d1272cae .
Upstream copyright and authorship remain with the MoviePilot contributors.
The upstream root LICENSE at this revision is GNU GPL version 3;
the complete unmodified text is retained as LICENSE in this directory.
This notice covers only the embedded official sources and the three source
fixtures below, not the repository's other plugins or code.

The first thirteen entries are the SOURCES literals in
`tests/test_quarkdisk_v3_host_sources.py`; the last three are complete files
under `tests/fixtures/quarkdisk_v3_consumers/`. Alias filenames are test-local
labels, not upstream module paths. All sixteen original byte sequences were
verified against the pinned upstream GitHub Contents API before publication.
Tests extract fixed AST bodies after digest checks; full-source Windows shell
commands are retained as upstream data, not executed by these tests.
No fixture contains running-instance configuration or account data.

| Fixture key / relative path | Upstream path | SHA-256 |
| --- | --- | --- |
| `app/schemas/types.py` | `app/schemas/types.py` | `7e890ad837f855cfebed048ad16c66861e2504f3a3c047a467ac284316ce8ed0` |
| `app/schemas/file.py` | `app/schemas/file.py` | `0670887f16b96ff84114415896c5c74de6c8f94de7c152f28cacb7f2e17fa5dc` |
| `app/schemas/exception.py` | `app/schemas/exception.py` | `e5b922de281aac5f20c4e39281210042b610b5e441514466db0d186e2de8a2a3` |
| `app/chain/storage.py` | `app/chain/storage.py` | `a95949dbcb54ae2c4979dab0733bc99938edb459c14da7d02f4c53980cc4381f` |
| `app/modules/filemanager/transhandler.py` | `app/modules/filemanager/transhandler.py` | `013ca7ae6baca6640b7c852449932b96ba7a49cff4b4322559f30c89139cc271` |
| `app/monitor/snapshot.py` | `app/monitor/snapshot.py` | `915020eaf894972fa5cb628f0fac3da9250c41d6434641a0218ee50e0804f209` |
| `app/runtime/extensions/module/contracts.py` | `app/runtime/extensions/module/contracts.py` | `8df039306a55d8ebfac88ba7ea738661015ccb899dd4bf49796eb6b4248293bc` |
| `host_http.py` | `app/adapters/network/http.py` | `514f61a6c3def9d3a0bf5ab502fc7e0c1fdde153e90dda9d9bfa7869034ff61c` |
| `app_foundation_url.py` | `app/foundation/url.py` | `2bafaf559d9f66df0c0faf2907280364918333a488cc5b3a9c0d555faaddef0c` |
| `app_foundation_singleton.py` | `app/foundation/singleton.py` | `28baffb3e0c560a89564bbe96cb010c1bdd373422fbcf1c541af7111ef924362` |
| `app_foundation_version.py` | `app/foundation/version.py` | `772e9bcf2a55e1dd0c45e9a68caea109043a562040b495cff88880978ea4e4aa` |
| `app_domain_plugin.py` | `app/domain/plugin.py` | `f5956924cd57c254ea677beae4d0478453db943652a35bbb91134542632eb3f4` |
| `host_market_client.py` | `app/adapters/external/plugin/client.py` | `f8b708a25bca50158dae6eefdd1732e7c85db1de57d365b358ca7468a34e697c` |
| `app/adapters/system/host.py` | `app/adapters/system/host.py` | `1d7b7c5526d3dfbab095b3dd2bec1d380bf8b61bdfd8bbabde538f1a93baff89` |
| `app/api/endpoints/storage.py` | `app/api/endpoints/storage.py` | `02ec671ff937501debf06aa898bbcc58071210e55e586a29b4657ec26cec76cc` |
| `app/startup/initializers/modules.py` | `app/startup/initializers/modules.py` | `4a6726aa0e83ce58a0820920a9e4eb946fc8ea41b4fe83f62e0026ed7d6ae25f` |
