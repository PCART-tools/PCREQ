![image](overview.png)

## What is PCREQ?
PCREQ is an automated tool for inferring compatible requirements for Python third-party library (TPL) upgrades. It is the first to achieve a fully automated process (end-to-end) that includes `knowledge acquisition`, `version compatibility assessment`, `invoked APIs and modules extraction`, `code compatibility assessment`, and `missing TPL completion`. PCREQ specializes in generating a compatible `requirements.txt` when upgrading a target TPL through a comprehensive analysis of both version and code compatibility issues.


For PCREQ's design and implementation details, please refer to our [paper](https://doi.org/10.1145/3806394).

## Evaluation
[REQBench](https://github.com/PCART-tools/REQBench) Benchmark for Python Third-Party Libraries Upgrade.

PCREQ's evaluation results are available [here](https://github.com/PCART-tools/PCREQ-evaluation).

## Usage
### Prerequisites
- Linux (tested on Ubuntu 18.04.1)
- Python 3.9.12
- thefuzz 0.22.1
- z3-solver 4.13.4.0
- packaging 24.0
- requests 2.32.3

### Step 1: Write the Configuration File
In the `configure` directory, construct a JSON configuration file, which should include the following information:
```json
{
    "projPath": "/home/usr/project",
    "requirementsPath": "/home/usr/requirements.txt",
    "targetLibrary": "pillow",
    "startVersion": "6.2.0",
    "targetVersion": "7.0.0",
    "pythonVersion": "3.7",
    "knowledgePath": "/home/usr/knowledge/"
}
```
`projPath`: project path, `requirementsPath`: requirements path, `targetLibrary`: target third-party library name, `startVersion`: current version of target third-party library, `targetVersion`: target version of target third-party library,  `pythonVersion`: the Python version, `knowledgePath`: the path of knowledge.

Given the potentially large knowledge size, `knowledgePath` supports custom storage locations.


### Step 2: Knowledge Acquisition
The extracted knowledge (Third-party library source code) of REQBench can be accessed at [libraries-Part1](https://doi.org/10.5281/zenodo.16150223), [libraries-Part2](https://doi.org/10.5281/zenodo.16152703), [libraries-Part3](https://doi.org/10.5281/zenodo.16152831).
To use the downloaded knowledge, run:
```bash
cd /home/usr/knowledge/
mkdir libraries
tar -xzvf *.tar.gz -C libraries
```

The extracted knowledge (extracted code entities, candidate versions, and version constraints) of REQBench can be accessed at [Entities, Versions, Constraints](https://doi.org/10.5281/zenodo.16160351).
To use the downloaded knowledge, run:
```bash
cd /home/usr/knowledge/
tar -xzvf *.tar.gz -C .
```

**Run Command**
```shell
python knowledge_acquisition.py --config config.json
```

### Step 3: Compatible Upgraded requirements.txt Generation
**Run Command**
```shell
python main.py --config config.json
```


## Usage Example
```shell
python knowledge_acquisition.py --config config.json
python main.py --config config.json
```

### Output

The output consists of two parts: the updated `requirements.txt` and an analysis report, which is located in the `report` directory and contains what PCREQ performed in terms of version and code compatibility checks and fixes. The contents of the report are as follows:
```
*************Upgrade pillow from 6.2.0 to 7.0.0 in PyTorch-ENet*************
Checking PyTorch-ENet is compatible with pillow7.0.0?...
PyTorch-ENet is compatible with pillow7.0.0
Checking torchvision0.3.0 is compatible with pillow7.0.0?...
torchvision0.3.0 is not compatible with pillow7.0.0-The PIL.PILLOW_VERSION is deprecated.
Checking torchvision0.4.0 is compatible with pillow7.0.0?...
torchvision0.4.0 is not compatible with pillow7.0.0-The PIL.PILLOW_VERSION is deprecated.
Checking PyTorch-ENet is compatible with torchvision0.4.0?...
PyTorch-ENet is compatible with torchvision0.4.0
Checking torchvision0.4.1 is compatible with pillow7.0.0?...
torchvision0.4.1 is not compatible with pillow7.0.0-The PIL.PILLOW_VERSION is deprecated.
Checking PyTorch-ENet is compatible with torchvision0.4.1?...
PyTorch-ENet is compatible with torchvision0.4.1
Checking torchvision0.4.2 is compatible with pillow7.0.0?...
torchvision0.4.2 is not compatible with pillow7.0.0-The PIL.PILLOW_VERSION is deprecated.
Checking PyTorch-ENet is compatible with torchvision0.4.2?...
PyTorch-ENet is compatible with torchvision0.4.2
Checking torchvision0.5.0 is compatible with pillow7.0.0?...
torchvision0.5.0 is compatible with pillow7.0.0
Checking PyTorch-ENet is compatible with torchvision0.5.0?...
PyTorch-ENet is compatible with torchvision0.5.0
Checking torchvision0.5.0 is compatible with torch1.4.0?...
torchvision0.5.0 is compatible with torch1.4.0
Checking PyTorch-ENet is compatible with torch1.4.0?...
PyTorch-ENet is compatible with torch1.4.0
*************New requirements.txt has been generated!*************
Time cost: 9.06177806854248 s
```

## Citation

If PCREQ supports your research or development work, please cite the following publication.

```
@article{PCREQ_TOSEM2026,
  author={Lei, Huashan and Xiao, Guanping and Liu, Yepang and Zheng, Zheng},
  journal={ACM Transactions on Software Engineering and Methodology},
  title={PCREQ: Automated Inference of Compatible Requirements for Python Third-party Library Upgrades},
  year={2026},
  doi={10.1145/3806394}
}
```

## License

PCREQ is licensed under the GNU Affero General Public License v3.0. See [LICENSE.txt](./LICENSE.txt) for details.

