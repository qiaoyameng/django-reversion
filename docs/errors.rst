.. _errors:

Errors
======

django-reversion defines several custom errors.


.. _RegistrationError:

reversion.RegistrationError
---------------------------

Something went wrong with the :ref:`registration-api`.


.. _RevisionManagementError:

reversion.RevisionManagementError
---------------------------------

Something went wrong using the :ref:`revision-api`.


.. _RevertError:

reversion.RevertError
---------------------

Something went wrong reverting a revision.


.. _DataAdapterError:

reversion.DataAdapterError
--------------------------

Something went wrong upgrading old version data via the :ref:`data-adapter-api`, e.g. a missing adapter step, a cycle in the adapter chain, or a failing adapter callback. A subclass of :ref:`RevertError`.
