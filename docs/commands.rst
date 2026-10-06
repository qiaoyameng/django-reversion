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
    # Also keep restore points tagged in the revision comment.
    ./manage.py deleterevisions your_app.YourModel --keep=3 --keep-comment="release"
    # Preview the retention plan without deleting anything.
    ./manage.py deleterevisions your_app.YourModel --keep=3 --dry-run

Run ``./manage.py deleterevisions --help`` for more information.

The command computes a retention plan before deleting anything. The plan
keeps the most recent ``--keep`` versions of each object and any restore
points tagged with ``--keep-comment``, then keeps everything those
targets relationally depend on: versions sharing a revision with a kept
version (a revision is the atomic unit of a revert), and revisions still
referenced by revision metadata. Every kept version and revision is
listed with its retention reason at higher verbosity. With ``--dry-run``,
the plan is printed without deleting anything. When the plan is executed,
the deletion runs in a transaction and first checks that the underlying
version set has not changed since the plan was built, so versions written
after the preview are never deleted by accident.

.. Warning::
    With no arguments, this command will delete your entire revision history! Read the command help for ways to limit which revisions should be deleted.
