# RizomUVLink

An open source Python module to control a RizomUV Standalone instance from a DCC

## Description

RizomUVLink is a Python module made to control a RizomUV Standalone instance from any Python capable DCC or from any Python program.

It consists from a set of compiled Python modules, some third party compiled libraries and some Python code. The libraries are present in several version, one for each version of Python and for each OS.

It has the following features:
* Permits the control of a RizomUV instance thru its API's features directly from Python code running on the DCC
* Allows creation of "live link type bridges" using fast memory transfers from DCC to RizomUV and vice & versa
* Provides method helpers to facilitate most used tasks, i.e: launch the most recent RizomUV version on the OS
* Detect connection losts (RizomUV crash for instance) to prevent infinite loops on the DCC's plugin

The main principle is that commands and data can be emitted using the RizomUVLink module and they will be transfered (by IPC) to a specified RizomUV Standalone running instance. Data present in the RizomUV instance can also be retreived from RizomUVLink.

## Getting Started

### Dependencies

* RizomUV Standalone version 2022.2 or superior, available at https://rizom-lab.com
* Python 3.6.X | 3.7.X | 3.8.X | 3.9.X | 3.10.X (tell us if you need other versions of Python)

### Installing

All files constitute the module for all platforms and versions of Python. The installation is the full repository folder's content by itself.

## Help

Please have a look at the **examples** folder, especially at the **Simple.py** file.

## Authors

Remi Arquier from Rizom-Lab

## License

This project is licensed under the **MIT License** - see the LICENSE.md file for details

MIT License is a short and simple permissive license with conditions only requiring preservation of copyright and license notices. Licensed works, modifications, and larger works may be distributed under different terms and without source code. It allows a commercial use.

## Acknowledgments

* [ZeroMQ](https://https://zeromq.org/)
