# Data

<a id="table-of-contents"></a>
## Table of Contents

- [Purpose](#purpose)

## Purpose

This directory holds the local SQLite database `market-data.sqlite3`. The `collect` command creates it on first use; the `show` command only reads it and never creates it. Do not commit the database or share it: it contains data from public sources, and the reuse conditions of most of them are not established. The first collection of a large US company can add 20 to 40 MB, because the company's reported values are stored once. Delete the file only when you intend to discard every stored observation, because the collector never deletes or replaces stored data itself. The parent [README](../README.md) explains how to run the commands.

[↑ Back to Table of Contents](#table-of-contents)
