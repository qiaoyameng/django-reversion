.. _commands:

Management commands
===================

django-reversion includes a number of ``django-admin.py`` management commands.


.. _createinitialrevisions:

createinitialrevisions
----------------------

Creates an initial revision for all registered models in your project. It should be run after installing django-reversion, or registering a new model with django-reversion.

.. code:: bash

    ./manage.py createinitialrevisions
    ./manage.py createinitialrevisions your_app.YourModel --comment="Initial revision."
    ./manage.py createinitialrevisions your_app.YourModel --meta="{\"your_app.RevisionMeta\": {\"hello\": \"world\"}}"

Run ``./manage.py createinitialrevisions --help`` for more information.

.. Warning::
    For large databases, this command can take a long time to run.


deleterevisions
---------------

Deletes old revisions. It can be run regularly to keep revision history manageable.

.. code:: bash

    ./manage.py deleterevisions
    # keep any changes from last 30 days
    ./manage.py deleterevisions your_app.YourModel --days=30
    # keep 30 most recent changes for each item.
    ./manage.py deleterevisions your_app.YourModel --keep=30
    # Keep anything from last 30 days and at least 3 from older changes.
    ./manage.py deleterevisions your_app.YourModel --keep=3 --days=30

Run ``./manage.py deleterevisions --help`` for more information.

.. Warning::
    With no arguments, this command will delete your entire revision history! Read the command help for ways to limit which revisions should be deleted.

retainrevisions
---------------

A safety-aware alternative to ``deleterevisions``. It builds a *retention plan*
that guarantees every object keeps enough history to be reverted:

* the ``--keep N`` most recent versions of every object are always retained;
* every revision whose comment contains a ``--marker`` substring is kept as a
  named recovery point (the option can be repeated);
* revisions shared between several objects are kept whole whenever one of
  their versions is retained, so reversion never loses a required sibling;
* revisions referenced by revision metadata are never deleted;
* when app/model arguments are supplied, a revision containing versions of
  models outside the requested scope is protected rather than partially pruned.

The command always runs as a dry run first and prints the exact
``Revision``/``Version`` rows that would be deleted, together with the reason
each retained row is kept. Pass ``--execute`` to perform the deletion inside a
single transaction.

.. code:: bash
    # Preview what would be pruned, keeping one version per object
    ./manage.py retainrevisions your_app.YourModel --keep=1
    # Keep named recovery points as well, then execute
    ./manage.py retainrevisions your_app.YourModel \
        --keep=1 --marker="release" --execute
    # Only prune revisions older than 30 days
    ./manage.py retainrevisions your_app.YourModel --keep=1 --days=30

To guard against revisions written after the preview, the plan records a
fingerprint of the revision tables. With ``--plan-file`` the dry run writes the
plan to disk and ``--execute`` re-validates that fingerprint before deleting;
execution aborts (and nothing is deleted) if anything changed. The same check is
performed by the :ref:`programmatic API <api>`, see
``reversion.build_retention_plan`` and ``reversion.execute_retention_plan``.
